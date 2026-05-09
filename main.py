import os
import re
import sys
import time
import threading
import msvcrt
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from tqdm import tqdm
from datetime import datetime
from colorama import Fore, Style, init

init()

# ── Keyboard listener (Ctrl+D opens exclusion menu) ──────────────────────────

_menu_event = threading.Event()   # set when user presses Ctrl+D
_stop_kbd   = threading.Event()   # set to stop the listener thread
_MENU_SIGNAL = object()           # sentinel returned by download_file

def _kbd_listener():
    while not _stop_kbd.is_set():
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch == b'\x04':   # Ctrl+D
                _menu_event.set()
        time.sleep(0.05)

# ── Console helper ────────────────────────────────────────────────────────────

class Console:
    _C = {
        'lb': Fore.LIGHTBLACK_EX,  'lr': Fore.LIGHTRED_EX,   'lg': Fore.LIGHTGREEN_EX,
        'ly': Fore.LIGHTYELLOW_EX, 'lb2': Fore.LIGHTBLUE_EX, 'lm': Fore.LIGHTMAGENTA_EX,
        'lc': Fore.LIGHTCYAN_EX,   'w': Fore.WHITE,           'R': Style.RESET_ALL,
    }

    def _ts(self):
        return datetime.now().strftime("%H:%M:%S")

    def _print(self, lvl_color, lvl, msg, obj):
        c = self._C
        tqdm.write(f"{c['lb']}{self._ts()} » {lvl_color}{lvl} {c['lb']}• {c['w']}{msg} : {lvl_color}{obj}{c['R']}")

    def clear(self):
        os.system("cls" if os.name == "nt" else "clear")

    def success(self, m, o): self._print(self._C['lg'],  'SUCC', m, o)
    def error(self, m, o):   self._print(self._C['lr'],  'ERRR', m, o)
    def warning(self, m, o): self._print(self._C['ly'],  'WARN', m, o)
    def info(self, m, o):    self._print(self._C['lb2'], 'INFO', m, o)
    def done(self, m, o):    self._print(self._C['lm'],  'DONE', m, o)

    def prompt(self, message):
        c = self._C
        return input(f"{c['lb']}{self._ts()} » {c['lc']}INPUT {c['lb']}• {c['w']}{message}{c['R']}")

log = Console()

# ── HTTP headers ──────────────────────────────────────────────────────────────

HEADERS = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'accept-language': 'en-US,en;q=0.5',
    'referer': 'https://fitgirl-repacks.site/',
    'sec-ch-ua': '"Brave";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
}

BAR_FMT = '{desc} {percentage:3.0f}%|{bar:28}| {n_fmt}/{total_fmt} [{rate_fmt}, ETA {remaining}]'

# ── Scraping helpers ──────────────────────────────────────────────────────────

def scrape_fitgirl(fitgirl_url):
    """Fetch a FitGirl repack page and return (game_name, [fuckingfast_urls])."""
    log.info("Fetching FitGirl page", fitgirl_url)
    try:
        r = requests.get(fitgirl_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to fetch FitGirl page", str(e))
        sys.exit(1)

    soup = BeautifulSoup(r.text, 'html.parser')

    # Game name from page title  e.g. "It Takes Two » FitGirl Repacks"
    title_tag = soup.find('title')
    if title_tag:
        raw = title_tag.text.split('»')[0].strip()
        game_name = re.sub(r'[\\/:*?"<>|]', '', raw).strip()
    else:
        game_name = urlparse(fitgirl_url).path.strip('/').split('/')[-1].replace('-', ' ').title()

    links = [
        a['href']
        for div in soup.find_all('div', class_='dlinks')
        for a in div.find_all('a', href=True)
        if a['href'].startswith('https://fuckingfast.co/')
    ]

    if not links:
        log.error("No fuckingfast.co links found on page", "check the URL and retry")
        sys.exit(1)

    log.success("Found download links", len(links))
    return game_name, links


def resolve_fuckingfast(ff_url):
    """Return (filename, direct_download_url) from a fuckingfast.co page."""
    try:
        r = requests.get(ff_url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        log.error("Failed to fetch fuckingfast page", str(e))
        return None, None

    soup = BeautifulSoup(r.text, 'html.parser')
    meta = soup.find('meta', attrs={'name': 'title'})
    file_name = meta['content'] if meta else os.path.basename(urlparse(ff_url).path)

    for script in soup.find_all('script'):
        if 'function download' in script.text:
            m = re.search(r"window\.open\([\"'](https?://[^\s\"'\)]+)", script.text)
            if m:
                return file_name, m.group(1)

    return file_name, None


def get_remote_size(url):
    """Return remote file size in bytes (0 if unknown)."""
    try:
        # HEAD is often ignored by CDNs; use GET+stream and close immediately
        r = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        size = int(r.headers.get('content-length', 0))
        r.close()
        return size
    except Exception:
        return 0

# ── Download ──────────────────────────────────────────────────────────────────

def download_file(url, output_path, file_index, total_files, overall_bar):
    """
    Download *url* to *output_path* with resume support.
    Drives both a per-file tqdm bar (position=1) and the shared overall_bar (position=0).
    Returns True on success.
    """
    existing = os.path.getsize(output_path) if os.path.exists(output_path) else 0

    req_headers = dict(HEADERS)
    if existing > 0:
        req_headers['Range'] = f'bytes={existing}-'
        log.info("Resuming", f"{os.path.basename(output_path)} ({existing:,} bytes already done)")

    try:
        r = requests.get(url, headers=req_headers, stream=True, timeout=60)
    except requests.RequestException as e:
        log.error("Request failed", str(e))
        return False

    # 416 = Range Not Satisfiable → file already fully downloaded
    if r.status_code == 416:
        log.success("Already complete", os.path.basename(output_path))
        return True

    if r.status_code not in (200, 206):
        log.error("Unexpected HTTP status", r.status_code)
        return False

    chunk_total = int(r.headers.get('content-length', 0))
    file_total  = existing + chunk_total

    short = os.path.basename(output_path)
    short = (short[:37] + '...') if len(short) > 40 else short
    desc  = f"{Fore.CYAN}[{file_index}/{total_files}] {short}{Style.RESET_ALL}"

    file_bar = tqdm(
        total=file_total,
        initial=existing,
        unit='B',
        unit_scale=True,
        unit_divisor=1024,
        desc=desc,
        position=1,
        leave=False,
        bar_format=BAR_FMT,
        dynamic_ncols=True,
        colour='cyan',
    )

    mode = 'ab' if existing > 0 else 'wb'
    menu_requested = False
    try:
        with open(output_path, mode) as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
                    file_bar.update(len(chunk))
                    overall_bar.update(len(chunk))
                    if _menu_event.is_set():
                        menu_requested = True
                        break
    except KeyboardInterrupt:
        raise
    finally:
        file_bar.close()

    if menu_requested:
        return _MENU_SIGNAL
    return True

# ── Exclusion menu ───────────────────────────────────────────────────────────

def show_exclusion_menu(sizes, excluded, overall_bar):
    overall_bar.clear()
    C = Console._C
    tqdm.write(f"\n{Fore.LIGHTMAGENTA_EX}{chr(9472) * 62}")
    tqdm.write(f"  File List  —  enter numbers to toggle exclusion, Enter to resume")
    tqdm.write(f"{chr(9472) * 62}{Style.RESET_ALL}")
    for i, (fname, durl, out, existing, remote) in enumerate(sizes):
        existing_now = os.path.getsize(out) if os.path.exists(out) else 0
        if i in excluded:
            status = f"{Fore.LIGHTRED_EX} SKIP {Style.RESET_ALL}"
        elif remote > 0 and existing_now >= remote:
            status = f"{Fore.LIGHTGREEN_EX} DONE {Style.RESET_ALL}"
        else:
            pct = int(existing_now * 100 / remote) if remote > 0 else 0
            status = f"{Fore.LIGHTBLUE_EX}{pct:3d}%{Style.RESET_ALL} "
        short = fname[:52] + ('...' if len(fname) > 52 else '')
        tqdm.write(f"  {Fore.LIGHTBLACK_EX}[{i+1:02d}]{Style.RESET_ALL} {status}  {short}")
    tqdm.write(f"\n{Fore.LIGHTYELLOW_EX}Numbers (e.g. 3,5-7) to toggle, or Enter to resume:{Style.RESET_ALL}")
    try:
        raw = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    for part in raw.replace(' ', '').split(','):
        if '-' in part:
            bounds = part.split('-')
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                for n in range(int(bounds[0]), int(bounds[1]) + 1):
                    idx = n - 1
                    if 0 <= idx < len(sizes):
                        excluded.discard(idx) if idx in excluded else excluded.add(idx)
        elif part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(sizes):
                excluded.discard(idx) if idx in excluded else excluded.add(idx)
    tqdm.write(f"{Fore.LIGHTMAGENTA_EX}{chr(9472) * 62}{Style.RESET_ALL}\n")
    overall_bar.refresh()

# ── Entry point ───────────────────────────────────────────────────────────────

def fmt_bytes(b):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def main():
    log.clear()
    tqdm.write(f"{Fore.LIGHTMAGENTA_EX}{'─' * 62}")
    tqdm.write(f"  FitGirl Easy Downloader")
    tqdm.write(f"{'─' * 62}{Style.RESET_ALL}\n")

    fitgirl_url = log.prompt("Enter FitGirl game URL : ").strip()
    if not fitgirl_url:
        log.error("No URL provided", "exiting")
        sys.exit(1)

    # 1 — scrape FitGirl page for fuckingfast links
    game_name, ff_links = scrape_fitgirl(fitgirl_url)
    downloads_folder = os.path.join("downloads", game_name)
    os.makedirs(downloads_folder, exist_ok=True)
    log.info("Download folder", downloads_folder)

    # 2 — resolve each fuckingfast page → direct download URL (parallel)
    WORKERS = min(16, len(ff_links))
    log.info("Resolving direct URLs", f"{len(ff_links)} links  (workers: {WORKERS})")
    resolved_map = {}  # index → (fname, durl)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(resolve_fuckingfast, url): i for i, url in enumerate(ff_links)}
        for fut in as_completed(futures):
            i   = futures[fut]
            url = ff_links[i]
            fname, durl = fut.result()
            if durl:
                resolved_map[i] = (fname, durl)
                log.success(f"Resolved [{len(resolved_map)}/{len(ff_links)}]", fname)
            else:
                log.warning(f"Could not resolve", url)

    # keep original order
    resolved = [resolved_map[i] for i in sorted(resolved_map)]

    if not resolved:
        log.error("No resolvable download URLs found", "exiting")
        sys.exit(1)

    # 3 — fetch file sizes in parallel
    log.info("Fetching file sizes", f"{len(resolved)} files  (workers: {WORKERS})")

    def _size_entry(item):
        fname, durl = item
        out      = os.path.join(downloads_folder, fname)
        existing = os.path.getsize(out) if os.path.exists(out) else 0
        remote   = get_remote_size(durl)
        return (fname, durl, out, existing, remote)

    sizes_map = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_size_entry, item): i for i, item in enumerate(resolved)}
        for fut in as_completed(futures):
            sizes_map[futures[fut]] = fut.result()

    sizes = [sizes_map[i] for i in range(len(resolved))]
    total_remote   = sum(s[4] for s in sizes)
    total_existing = sum(s[3] for s in sizes)

    log.info("Total size",         fmt_bytes(total_remote) if total_remote else "unknown")
    log.info("Already downloaded",  fmt_bytes(total_existing))
    log.info("Remaining",           fmt_bytes(max(0, total_remote - total_existing)) if total_remote else "unknown")
    tqdm.write("")

    # 4 — download with dual progress bars
    # If we couldn't fetch sizes, use total=None (spinner) to avoid a broken 0/0 bar
    overall_total = total_remote if total_remote > 0 else None
    overall_bar = tqdm(
        total=overall_total,
        initial=total_existing if overall_total else 0,
        unit='B',
        unit_scale=True,
        unit_divisor=1024,
        desc=f"{Fore.LIGHTMAGENTA_EX}Overall  [{len(resolved)} files]{Style.RESET_ALL}",
        position=0,
        leave=True,
        bar_format=BAR_FMT if overall_total else '{desc} {n_fmt} [{rate_fmt}]',
        dynamic_ncols=True,
        colour='magenta',
    )

    # start keyboard listener
    _stop_kbd.clear()
    kbd_thread = threading.Thread(target=_kbd_listener, daemon=True)
    kbd_thread.start()
    tqdm.write(f"{Fore.LIGHTBLACK_EX}  Tip: press Ctrl+D to open the file list and exclude files{Style.RESET_ALL}\n")

    excluded = set()
    success_count = 0
    i = 0
    while i < len(sizes):
        fname, durl, out, existing, remote = sizes[i]

        if i in excluded:
            log.warning(f"Skipped [{i+1}/{len(sizes)}]", fname)
            i += 1
            continue

        # refresh existing size in case a previous run partially downloaded it
        existing = os.path.getsize(out) if os.path.exists(out) else 0
        if remote > 0 and existing >= remote:
            log.success(f"Already complete [{i+1}/{len(sizes)}]", fname)
            success_count += 1
            i += 1
            continue

        try:
            result = download_file(durl, out, i + 1, len(sizes), overall_bar)
        except KeyboardInterrupt:
            _stop_kbd.set()
            overall_bar.close()
            tqdm.write("")
            log.warning("Download interrupted", "progress saved — rerun to continue")
            sys.exit(0)
        except Exception as e:
            log.error(f"Error [{i+1}/{len(sizes)}]", str(e))
            i += 1
            continue

        if result is _MENU_SIGNAL:
            _menu_event.clear()
            show_exclusion_menu(sizes, excluded, overall_bar)
            # don't advance i — retry current file unless it was just excluded
            continue
        elif result is True:
            success_count += 1
            log.success(f"Done [{i+1}/{len(sizes)}]", fname)
        else:
            log.error(f"Failed [{i+1}/{len(sizes)}]", fname)
        i += 1

    _stop_kbd.set()
    overall_bar.close()
    tqdm.write("")
    log.done("All done", f"{success_count}/{len(sizes)} files  →  {downloads_folder}")


if __name__ == '__main__':
    main()


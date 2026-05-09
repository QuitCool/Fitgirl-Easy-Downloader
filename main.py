import json
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

# ── Shared state ──────────────────────────────────────────────────────────────

_menu_trigger  = threading.Event()   # kbd → main: open menu now
_menu_open     = threading.Event()   # main → dl: terminal in menu mode
_stop_all      = threading.Event()   # global Ctrl+C shutdown
_dl_done       = threading.Event()   # download thread finished
_state_lock    = threading.Lock()    # protects `excluded` set
_bytes_written = [0]                 # bytes written this session (GIL-safe)
_log_queue     = []                  # log lines buffered while menu is open

# ── Keyboard listener ─────────────────────────────────────────────────────────

def _kbd_listener():
    while not _stop_all.is_set():
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch == b'\x04' and not _menu_open.is_set():  # Ctrl+D
                _menu_trigger.set()
            elif ch == b'\x03':  # Ctrl+C
                _stop_all.set()
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
        line = f"{c['lb']}{self._ts()} » {lvl_color}{lvl} {c['lb']}• {c['w']}{msg} : {lvl_color}{obj}{c['R']}"
        if _menu_open.is_set():
            _log_queue.append(line)
        else:
            tqdm.write(line)

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

# ── Download worker (background thread) ──────────────────────────────────────

def _download_worker(sizes, excluded, results, overall_bar_ref):
    """
    Runs in a daemon thread. Iterates sizes sequentially, skipping excluded.
    overall_bar_ref is a list so the menu can swap the bar after rebuilding it.
    """
    n = len(sizes)
    for i, (fname, durl, out, _initial_existing, remote) in enumerate(sizes):
        if _stop_all.is_set():
            break

        with _state_lock:
            skip = i in excluded
        if skip:
            results[i] = 'skip'
            log.warning(f"Skipped [{i+1}/{n}]", fname)
            continue

        existing = os.path.getsize(out) if os.path.exists(out) else 0
        if remote > 0 and existing >= remote:
            results[i] = True
            log.success(f"Already complete [{i+1}/{n}]", fname)
            continue

        req_headers = dict(HEADERS)
        if existing > 0:
            req_headers['Range'] = f'bytes={existing}-'
            log.info("Resuming", f"{fname} ({existing:,} bytes already done)")

        try:
            r = requests.get(durl, headers=req_headers, stream=True, timeout=60)
        except requests.RequestException as e:
            log.error("Request failed", str(e))
            results[i] = False
            continue

        if r.status_code == 416:
            log.success("Already complete", fname)
            results[i] = True
            continue

        if r.status_code not in (200, 206):
            log.error("Unexpected HTTP status", r.status_code)
            results[i] = False
            continue

        chunk_total = int(r.headers.get('content-length', 0))
        file_total  = existing + chunk_total
        short = fname[:37] + ('...' if len(fname) > 40 else '')
        desc  = f"{Fore.CYAN}[{i+1}/{n}] {short}{Style.RESET_ALL}"

        file_bar = tqdm(
            total=file_total, initial=existing,
            unit='B', unit_scale=True, unit_divisor=1024,
            desc=desc, position=1, leave=False,
            bar_format=BAR_FMT, dynamic_ncols=True, colour='cyan',
        )

        ok   = True
        mode = 'ab' if existing > 0 else 'wb'
        try:
            with open(out, mode) as f:
                for chunk in r.iter_content(8192):
                    if _stop_all.is_set():
                        ok = False
                        break
                    if chunk:
                        f.write(chunk)
                        _bytes_written[0] += len(chunk)
                        if not _menu_open.is_set():
                            file_bar.update(len(chunk))
                            overall_bar_ref[0].update(len(chunk))
                        with _state_lock:
                            if i in excluded:
                                ok = 'excluded_mid'
                                break
        finally:
            file_bar.close()

        if ok is True:
            results[i] = True
            log.success(f"Done [{i+1}/{n}]", fname)
        elif ok == 'excluded_mid':
            results[i] = 'skip'
            log.warning(f"Excluded mid-download [{i+1}/{n}]", fname)
        else:
            results[i] = False
            if not _stop_all.is_set():
                log.error(f"Failed [{i+1}/{n}]", fname)

    _dl_done.set()

# ── Interactive file manager ──────────────────────────────────────────────────

_MENU_W = 68

def _file_status(i, sizes, excluded, results):
    """Returns (status_str, pct_int).  status: done|skip|passed|pending"""
    fname, durl, out, _e, remote = sizes[i]
    existing_now = os.path.getsize(out) if os.path.exists(out) else 0
    with _state_lock:
        is_excl = i in excluded
    if is_excl:
        return 'skip', (int(existing_now * 100 / remote) if remote > 0 else 0)
    res = results.get(i)
    if res is True or (remote > 0 and existing_now >= remote):
        return 'done', 100
    if res == 'skip':
        return 'passed', (int(existing_now * 100 / remote) if remote > 0 else 0)
    return 'pending', (int(existing_now * 100 / remote) if remote > 0 else 0)


def _render_menu(sizes, excluded, results, cursor, viewport_start, viewport_size):
    VIEWPORT = viewport_size
    n  = len(sizes)
    C  = Fore.LIGHTMAGENTA_EX
    Y  = Fore.LIGHTYELLOW_EX
    R  = Style.RESET_ALL

    buf = []
    buf.append('\033[H')   # cursor to top-left of alternate screen

    buf.append(f"{C}{'─' * _MENU_W}\033[K\n")
    buf.append(
        f"  File Manager  "
        f"{Y}↑↓{C} navigate  {Y}Space{C} toggle  {Y}Ctrl+X{C} save & resume\033[K\n"
    )
    buf.append(f"{'─' * _MENU_W}{R}\033[K\n")

    viewport_end = min(n, viewport_start + VIEWPORT)
    for i in range(viewport_start, viewport_end):
        fname  = sizes[i][0]
        status, pct = _file_status(i, sizes, excluded, results)
        is_cur = (i == cursor)

        if status == 'done':
            stat = f"{Fore.LIGHTGREEN_EX} DONE {R}"
        elif status == 'skip':
            stat = f"{Fore.LIGHTRED_EX} SKIP {R}"
        elif status == 'passed':
            stat = f"{Fore.LIGHTBLACK_EX} PASS {R}"
        else:
            stat = f"{Fore.LIGHTBLUE_EX}{pct:3d}% {R}"

        short      = fname[:50] + ('...' if len(fname) > 50 else '')
        idx        = f"{Fore.LIGHTBLACK_EX}[{i+1:02d}]{R}"
        mark       = f"  {Y}▶{R} " if is_cur else "    "
        name_col   = f"{Fore.WHITE}{short}{R}" if is_cur else short
        buf.append(f"{mark}{idx} {stat}  {name_col}\033[K\n")

    buf.append(
        f"  {Fore.LIGHTBLACK_EX}({viewport_start+1}–{viewport_end} of {n})  "
        f"scroll ↑↓{R}\033[K\n"
    )

    with _state_lock:
        n_excl = len(excluded)
    n_rem = sum(
        1 for j in range(n)
        if j not in excluded and results.get(j) not in (True, 'skip')
    )
    bw = _bytes_written[0]
    buf.append(f"{C}{'─' * _MENU_W}{R}\033[K\n")
    buf.append(
        f"  Excluded {Fore.LIGHTRED_EX}{n_excl}{R}"
        f"  │  Remaining {Fore.LIGHTCYAN_EX}{n_rem}{R}"
        f"  │  Downloaded {Fore.LIGHTGREEN_EX}{_fmt_bytes(bw)}{R}\033[K\n"
    )
    buf.append(f"  {Fore.LIGHTBLACK_EX}Download continues in the background…{R}\033[K\n")
    buf.append('\033[J')  # erase anything below

    sys.stdout.write(''.join(buf))
    sys.stdout.flush()


def show_interactive_menu(sizes, excluded, results, overall_bar_ref, skip_file=None):
    n        = len(sizes)
    cursor   = 0
    vp_start = 0
    _menu_open.set()

    def _vp_size():
        try:
            return max(3, os.get_terminal_size().lines - 7)
        except OSError:
            return 15

    VIEWPORT = _vp_size()

    # Enter alternate screen — own fixed canvas, never scrolls
    sys.stdout.write('\033[?1049h\033[H')
    sys.stdout.flush()
    _render_menu(sizes, excluded, results, cursor, vp_start, VIEWPORT)
    last_periodic = time.monotonic()

    while True:
        # Drain ALL pending keypresses; deduplicate Space to one toggle per cycle
        dirty        = False
        space_seen   = False

        while msvcrt.kbhit():
            ch = msvcrt.getch()

            if ch in (b'\xe0', b'\x00'):        # arrow / extended key
                if msvcrt.kbhit():
                    ch2 = msvcrt.getch()
                    if ch2 == b'H':              # ↑
                        cursor = (cursor - 1) % n
                    elif ch2 == b'P':            # ↓
                        cursor = (cursor + 1) % n
                    if cursor < vp_start:
                        vp_start = cursor
                    elif cursor >= vp_start + VIEWPORT:
                        vp_start = cursor - VIEWPORT + 1
                    dirty = True

            elif ch == b' ':                     # Space — record once, apply after drain
                space_seen = True

            elif ch in (b'\x18', b'\r', b'\n'): # Ctrl+X / Enter → exit
                while msvcrt.kbhit():
                    msvcrt.getch()
                cursor = -1    # sentinel
                break

        # Apply Space toggle exactly once (ignore key-repeat duplicates)
        if space_seen and cursor != -1:
            status, _ = _file_status(cursor, sizes, excluded, results)
            if status not in ('done', 'passed'):
                with _state_lock:
                    if cursor in excluded:
                        excluded.discard(cursor)
                    else:
                        excluded.add(cursor)
                dirty = True

        if cursor == -1:
            break

        # Periodic live refresh every 250 ms even without input
        now = time.monotonic()
        if dirty or now - last_periodic > 0.25:
            VIEWPORT = _vp_size()          # recheck on terminal resize
            _render_menu(sizes, excluded, results, cursor, vp_start, VIEWPORT)
            last_periodic = now

        time.sleep(0.01)   # 10 ms idle sleep

    # ── Restore progress display ──────────────────────────────────────────────
    overall_bar_ref[0].close()

    with _state_lock:
        excl_snap = set(excluded)

    new_total    = sum(s[4] for i, s in enumerate(sizes) if i not in excl_snap and s[4] > 0)
    new_existing = sum(
        (os.path.getsize(s[2]) if os.path.exists(s[2]) else 0)
        for i, s in enumerate(sizes) if i not in excl_snap
    )
    n_active = sum(
        1 for i in range(n)
        if i not in excl_snap and results.get(i) not in (True, 'skip')
    )

    # Leave alternate screen — original progress output is restored automatically
    sys.stdout.write('\033[?1049l')
    sys.stdout.flush()

    # Persist excluded filenames so they survive restarts
    if skip_file is not None:
        with _state_lock:
            excl_names = [sizes[i][0] for i in sorted(excluded) if i < len(sizes)]
        try:
            with open(skip_file, 'w', encoding='utf-8') as f:
                json.dump(excl_names, f, indent=2)
        except OSError:
            pass

    new_bar = tqdm(
        total=new_total if new_total > 0 else None,
        initial=new_existing if new_total > 0 else 0,
        unit='B', unit_scale=True, unit_divisor=1024,
        desc=f"{Fore.LIGHTMAGENTA_EX}Overall  [{n_active} files]{Style.RESET_ALL}",
        position=0, leave=True,
        bar_format=BAR_FMT if new_total > 0 else '{desc} {n_fmt} [{rate_fmt}]',
        dynamic_ncols=True, colour='magenta',
    )
    overall_bar_ref[0] = new_bar

    # flush buffered log lines
    _menu_open.clear()
    for line in _log_queue:
        tqdm.write(line)
    _log_queue.clear()
    tqdm.write(f"{Fore.LIGHTBLACK_EX}  Ctrl+D to open file manager{Style.RESET_ALL}")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_bytes(b):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.clear()
    tqdm.write(f"{Fore.LIGHTMAGENTA_EX}{'─' * 62}")
    tqdm.write(f"  FitGirl Easy Downloader")
    tqdm.write(f"{'─' * 62}{Style.RESET_ALL}\n")

    fitgirl_url = log.prompt("Enter FitGirl game URL : ").strip()
    if not fitgirl_url:
        log.error("No URL provided", "exiting")
        sys.exit(1)

    # 1 ── scrape FitGirl page
    game_name, ff_links = scrape_fitgirl(fitgirl_url)
    downloads_folder = os.path.join("downloads", game_name)
    os.makedirs(downloads_folder, exist_ok=True)
    log.info("Download folder", downloads_folder)

    # 2 ── resolve fuckingfast links in parallel
    WORKERS = min(16, len(ff_links))
    log.info("Resolving direct URLs", f"{len(ff_links)} links  (workers: {WORKERS})")
    resolved_map = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(resolve_fuckingfast, url): i for i, url in enumerate(ff_links)}
        for fut in as_completed(futures):
            i = futures[fut]
            fname, durl = fut.result()
            if durl:
                resolved_map[i] = (fname, durl)
                log.success(f"Resolved [{len(resolved_map)}/{len(ff_links)}]", fname)
            else:
                log.warning("Could not resolve", ff_links[i])

    resolved = [resolved_map[i] for i in sorted(resolved_map)]
    if not resolved:
        log.error("No resolvable download URLs found", "exiting")
        sys.exit(1)

    # 3 ── fetch file sizes in parallel
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

    log.info("Total size",         _fmt_bytes(total_remote) if total_remote else "unknown")
    log.info("Already downloaded",  _fmt_bytes(total_existing))
    log.info("Remaining",           _fmt_bytes(max(0, total_remote - total_existing)) if total_remote else "unknown")
    tqdm.write("")

    # 4 ── overall progress bar
    overall_total = total_remote if total_remote > 0 else None
    overall_bar = tqdm(
        total=overall_total,
        initial=total_existing if overall_total else 0,
        unit='B', unit_scale=True, unit_divisor=1024,
        desc=f"{Fore.LIGHTMAGENTA_EX}Overall  [{len(sizes)} files]{Style.RESET_ALL}",
        position=0, leave=True,
        bar_format=BAR_FMT if overall_total else '{desc} {n_fmt} [{rate_fmt}]',
        dynamic_ncols=True, colour='magenta',
    )
    overall_bar_ref = [overall_bar]

    tqdm.write(f"{Fore.LIGHTBLACK_EX}  Ctrl+D open file manager  │  Ctrl+C stop{Style.RESET_ALL}\n")

    # 5 ── shared state
    skip_file = os.path.join(downloads_folder, '.skip.json')
    excluded  = set()
    # Load previously skipped filenames and map back to current indices
    if os.path.exists(skip_file):
        try:
            with open(skip_file, 'r', encoding='utf-8') as f:
                skipped_names = set(json.load(f))
            for idx, s in enumerate(sizes):
                if s[0] in skipped_names:
                    excluded.add(idx)
            if excluded:
                log.info("Loaded skipped files", f"{len(excluded)} from previous session")
        except (OSError, json.JSONDecodeError):
            pass
    results  = {}

    # 6 ── start threads
    kbd_thread = threading.Thread(target=_kbd_listener, daemon=True)
    kbd_thread.start()

    dl_thread = threading.Thread(
        target=_download_worker,
        args=(sizes, excluded, results, overall_bar_ref),
        daemon=True,
    )
    dl_thread.start()

    # 7 ── main loop: handle menu triggers and Ctrl+C
    try:
        while not _dl_done.is_set() and not _stop_all.is_set():
            if _menu_trigger.wait(timeout=0.1):
                _menu_trigger.clear()
                show_interactive_menu(sizes, excluded, results, overall_bar_ref, skip_file)
    except KeyboardInterrupt:
        _stop_all.set()

    if _stop_all.is_set():
        overall_bar_ref[0].close()
        tqdm.write("")
        log.warning("Download interrupted", "progress saved — rerun to continue")
        sys.exit(0)

    dl_thread.join(timeout=5)
    _stop_all.set()  # stop kbd listener

    overall_bar_ref[0].close()
    tqdm.write("")
    success_count = sum(1 for v in results.values() if v is True)
    log.done("All done", f"{success_count}/{len(sizes)} files  →  {downloads_folder}")


if __name__ == '__main__':
    main()


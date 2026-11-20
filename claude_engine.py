import os
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from patchright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SESSION_FILE = "session_claude.json"

def split_telegram_text(text: str, limit: int = 4000):
    """Pecah teks panjang jadi chunk <= limit karakter, putus di batas
    paragraf/baris/kata supaya tidak memotong kata di tengah."""
    if len(text) <= limit:
        return [text]
    chunks = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]

def _send_one_chunk(text: str, chunk_no: int, total: int):
    """Kirim 1 chunk jawaban sebagai pesan teks (tanpa parse_mode supaya
    karakter < > di jawaban tidak bikin Telegram nolak). Retry 429 max 3x
    pakai retry_after dari Telegram; 400/error lain dicatat, bukan ditelan."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TG] No token/chat_id configured (chunk {chunk_no}/{total}).")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, 4):
        try:
            data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2.0
                try:
                    body = json.loads(e.read().decode("utf-8"))
                    wait = float(body.get("parameters", {}).get("retry_after", 2))
                except Exception:
                    pass
                print(f"[TG] chunk {chunk_no}/{total}: 429 (coba {attempt}/3), tunggu {wait}s lalu retry")
                time.sleep(wait)
                continue
            print(f"[TG Error] chunk {chunk_no}/{total}: HTTP {e.code}")
            return False
        except Exception as e:
            print(f"[TG Error] chunk {chunk_no}/{total}: {e}")
            return False
    print(f"[TG Error] chunk {chunk_no}/{total}: gagal setelah 3x retry (429)")
    return False

def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] No token/chat_id configured.")
        return
    chunks = split_telegram_text(text)
    for i, chunk in enumerate(chunks):
        _send_one_chunk(chunk, i + 1, len(chunks))
        if len(chunks) > 1 and i < len(chunks) - 1:
            time.sleep(2.0)
    if len(chunks) > 1:
        print(f"[TG] Jawaban panjang dipecah jadi {len(chunks)} pesan (total {len(text)} karakter)")

def send_chat_screenshot(photo_path: str, caption: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if not os.path.exists(photo_path):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        body = []
        body.append(f"--{boundary}".encode())
        body.append(f'Content-Disposition: form-data; name="chat_id"'.encode())
        body.append(''.encode())
        body.append(str(TELEGRAM_CHAT_ID).encode())
        
        if caption:
            body.append(f"--{boundary}".encode())
            body.append(f'Content-Disposition: form-data; name="caption"'.encode())
            body.append(''.encode())
            body.append(caption[:1024].encode())

        filename = os.path.basename(photo_path)
        body.append(f"--{boundary}".encode())
        body.append(f'Content-Disposition: form-data; name="photo"; filename="{filename}"'.encode())
        body.append('Content-Type: image/png'.encode())
        body.append(''.encode())
        with open(photo_path, "rb") as f:
            body.append(f.read())
        body.append(f"--{boundary}--".encode())
        body.append(''.encode())

        data = b"\r\n".join(body)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(data))
        }
        req = urllib.request.Request(url, data=data, headers=headers)
        urllib.request.urlopen(req, timeout=15)
        print(f"[TG Photo Sent] {photo_path}")
    except Exception as e:
        print(f"[TG Photo Error] {e}")

def check_security_or_login(page):
    """Cek apakah muncul Arkose, Cloudflare, atau terlempar ke Login."""
    content = page.content().lower()
    title = page.title().lower()
    current_url = page.url.lower()

    if "arkose" in content or "funcaptcha" in content:
        return "ARKOSE_CAPTCHA"
    if "tunggu sebentar" in title or "just a moment" in title or "checking your browser" in content or "challenge_redirect" in current_url:
        return "CLOUDFLARE_WAIT"
    if "/login" in current_url or "sign in with" in content:
        return "NEED_LOGIN"
    return "OK"

def find_claude_composer(page):
    """Cari input composer Claude di UI."""
    selectors = [
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true']",
        "fieldset div[contenteditable='true']",
        "textarea[placeholder*='Reply' i]",
        "textarea[placeholder*='Ask' i]",
        "textarea",
        "[role='textbox']"
    ]
    for s in selectors:
        try:
            loc = page.locator(s)
            if loc.count() > 0:
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible(timeout=1000):
                        return item
        except Exception:
            continue
    return None

def dismiss_claude_modals(page):
    """Tutup onboarding / dialog popup jika ada."""
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
            btns = page.locator("button:has-text('Acknowledge'), button:has-text('Got it'), button:has-text('Accept'), button:has-text('Continue'), button[aria-label*='Close' i]")
            if btns.count() > 0:
                for i in range(min(btns.count(), 2)):
                    b = btns.nth(i)
                    if b.is_visible(timeout=500):
                        b.click(force=True)
                        time.sleep(0.5)
        except Exception:
            pass

def _claude_answer_len(page, main_baseline=0):
    """Panjang jawaban Claude yang sedang tampil. Ukur gelembung pesan
    assistant dulu; kalau belum ada (masih streaming awal), ukur isi <main>
    relatif terhadap baseline awal (prompt + UI statis) supaya pertumbuhan
    jawaban tetap kelihatan. Kembalikan 0 kalau belum ada apa-apa."""
    try:
        bubbles = page.locator("div.font-claude-message, [data-is-streaming='false'], div[class*='standard-markdown'], div[class*='font-claude']")
        if bubbles.count() > 0:
            txt = (bubbles.last.inner_text() or "").strip()
            if txt:
                return len(txt)
    except Exception:
        pass
    try:
        main = page.locator("main")
        if main.count() > 0:
            return max(0, len(main.first.inner_text() or "") - main_baseline)
    except Exception:
        pass
    return 0

def _claude_is_generating(page):
    """True kalau tombol Stop masih kelihatan (Claude masih streaming)."""
    try:
        stop_btn = page.locator("button[aria-label*='Stop' i]")
        return stop_btn.count() > 0 and stop_btn.first.is_visible(timeout=500)
    except Exception:
        return False

def wait_for_claude_answer_done(page, max_wait_sec=900):
    """Tunggu jawaban Claude sampai tuntas (inactivity-based, bukan cap buta).
    Selesai kalau panjang jawaban stabil 8 detik + tidak generating, atau
    stabil 60 detik (slow path kalau indikator Stop nyangkut). Idle 300 detik
    tanpa pertumbuhan = mentok. Cap absolut 900 detik. Diagnostik tiap 30 detik
    supaya loop buta kelihatan di log."""
    start = time.time()
    last_len = 0
    last_growth = time.time()
    stable_since = None
    seen_activity = False
    last_diag = 0
    # Baseline isi <main> di awal loop (prompt + UI statis) untuk fallback.
    main_baseline = 0
    try:
        m0 = page.locator("main")
        if m0.count() > 0:
            main_baseline = len(m0.first.inner_text() or "")
    except Exception:
        pass
    while True:
        now = time.time()
        elapsed = now - start
        cur_len = _claude_answer_len(page, main_baseline)
        generating = _claude_is_generating(page)
        if cur_len > 0 or generating:
            seen_activity = True
        if cur_len > last_len:
            last_len = cur_len
            last_growth = now
            stable_since = None
        else:
            if stable_since is None:
                stable_since = now
        if now - last_diag >= 30:
            print(f"[{int(elapsed)}s] len={cur_len} gen={generating}")
            last_diag = now
        if seen_activity and stable_since is not None and now - stable_since >= 8 and not generating:
            print(f"[wait] Selesai: jawaban stabil 8s, tidak generating ({cur_len} karakter)")
            return cur_len
        if seen_activity and stable_since is not None and now - stable_since >= 60:
            print(f"[wait] Selesai (slow path): jawaban stabil 60s ({cur_len} karakter)")
            return cur_len
        if now - last_growth >= 300:
            print(f"[wait] Idle 300s tanpa pertumbuhan, berhenti ({cur_len} karakter)")
            return cur_len
        if elapsed >= max_wait_sec:
            print(f"[{int(max_wait_sec)}s] Pengaman absolut tercapai ({cur_len} karakter)")
            return cur_len
        time.sleep(2)

def main():
    prompt = "Halo Claude, siapa kamu dan apa modelmu? Jawab singkat."
    if len(sys.argv) > 1 and sys.argv[1].strip():
        prompt = sys.argv[1].strip()

    print(f"👉 [Patchright Engine] Menyiapkan pertanyaan ke Claude: '{prompt}'...")

    # Proxy configuration (mendukung format http://user:pass@host:port)
    # Kosong -> fallback ke WARP (socks5 127.0.0.1:40000) seperti bot lain.
    proxy_url = os.environ.get("PROXY_URL") or "socks5://127.0.0.1:40000"
    proxy_cfg = {}
    if "@" in proxy_url:
        parsed = urllib.parse.urlparse(proxy_url)
        proxy_cfg = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            "username": parsed.username or "",
            "password": parsed.password or ""
        }
    else:
        proxy_cfg = {"server": proxy_url}

    print(f"🌐 Menggunakan Proxy Server: {proxy_cfg['server']}")

    with sync_playwright() as p:
        # Panggil Google Chrome resmi dengan Patchright C++ stealth
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            proxy=proxy_cfg,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,800"
            ]
        )

        context_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "locale": "id-ID",
            "timezone_id": "Asia/Jakarta"
        }

        if os.path.exists(SESSION_FILE):
            try:
                context_kwargs["storage_state"] = SESSION_FILE
                print("✓ Memuat sesi cookie persisten dari session_claude.json")
            except Exception as e:
                print(f"[Warn] Gagal memuat session_claude.json: {e}")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        print("🌐 [Patchright] Membuka https://claude.ai/new ...")
        try:
            page.goto("https://claude.ai/new", wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
            print(f"👉 URL setelah akses awal: {page.url}")

            # Loop tunggu jika ada challenge redirect Cloudflare Turnstile
            for w in range(20):
                sec_state = check_security_or_login(page)
                if sec_state == "CLOUDFLARE_WAIT":
                    print(f"⏳ Menunggu Cloudflare Turnstile diselesaikan oleh Patchright (detik {w*2})... URL: {page.url}")
                    # Patchright secara otomatis menyelesaikan Turnstile di level C++, kita berikan waktu reaktif
                    try:
                        frames = page.frames
                        for f in frames:
                            cb = f.locator("input[type='checkbox'], #challenge-stage input, .ctp-checkbox-label")
                            if cb.count() > 0 and cb.first.is_visible(timeout=500):
                                print("👉 Klik checkbox Turnstile...")
                                cb.first.click(force=True)
                    except Exception:
                        pass
                    time.sleep(2)
                else:
                    break
        except Exception as e:
            print(f"[Nav Error] {e}")

        page.screenshot(path="01_claude_landing.png")
        print(f"📄 Page Title: {page.title()} | URL: {page.url}")
        send_chat_screenshot("01_claude_landing.png", f"Snapshot Landing Claude (Status: {check_security_or_login(page)})")

        dismiss_claude_modals(page)

        sec_state = check_security_or_login(page)
        print(f"🔍 Status halaman: {sec_state} (URL: {page.url})")

        if sec_state == "ARKOSE_CAPTCHA":
            print("❌ Waduh! Arkose Captcha terdeteksi.")
            send_chat_screenshot("01_claude_landing.png", "❌ Claude: Terhadang Arkose Captcha.")
            browser.close()
            sys.exit(1)
        elif sec_state == "NEED_LOGIN":
            print("❌ Sesi cookie expired / terlempar ke Login.")
            send_chat_screenshot("01_claude_landing.png", "❌ Claude: Sesi cookie expired, terlempar ke Login.")
            browser.close()
            sys.exit(1)

        composer = None
        for attempt in range(10):
            composer = find_claude_composer(page)
            if composer:
                print(f"✓ Composer Claude ditemukan pada detik ke-{attempt * 2}!")
                break
            time.sleep(2)

        if not composer:
            print("❌ Input composer tidak ditemukan.")
            send_chat_screenshot("01_claude_landing.png", f"❌ Claude: Composer tidak ditemukan (Status: {sec_state}).")
            browser.close()
            sys.exit(1)

        # Simpan sesi terbaru
        try:
            context.storage_state(path=SESSION_FILE)
            print("✓ State cookie Claude berhasil disimpan.")
        except Exception as e:
            print(f"[Session Save Error] {e}")

        # Mode keep-alive (cron): sesi sudah terverifikasi + disimpan,
        # tidak perlu bertanya — biar tidak spam Telegram tiap 6 jam.
        # CATATAN: workflow mengisi env ini dari ekspresi
        # ${{ github.event_name == 'schedule' }} yang jadi string "true"/"false";
        # string "false" itu truthy di Python, jadi harus bandingkan nilainya.
        if os.environ.get("CLAUDE_KEEPALIVE", "").lower() == "true":
            print("[KEEP-ALIVE] Sesi Claude disegarkan, tanpa prompt.")
            browser.close()
            return

        print("✓ Dashboard aktif terverifikasi! Mengetik prompt ke Claude...")
        try:
            composer.click(force=True)
            time.sleep(0.5)
            composer.fill(prompt)
        except Exception:
            page.keyboard.type(prompt, delay=15)

        time.sleep(1)
        send_btn = page.locator("button[aria-label*='Send' i], button:has-text('Send')")
        if send_btn.count() > 0 and send_btn.first.is_enabled():
            send_btn.first.click()
        else:
            page.keyboard.press("Enter")

        print("✓ Pertanyaan terkirim! Menunggu streaming Claude...")
        time.sleep(6)

        wait_for_claude_answer_done(page)

        time.sleep(3)
        page.screenshot(path="02_claude_answer.png")

        answer = ""
        bubbles = page.locator("div.font-claude-message, [data-is-streaming='false'], div[class*='standard-markdown'], div[class*='font-claude']")
        if bubbles.count() > 0:
            for i in range(bubbles.count() - 1, -1, -1):
                txt = bubbles.nth(i).inner_text().strip()
                if txt and prompt not in txt and len(txt) > 10:
                    answer = txt
                    break

        if not answer:
            ps = page.locator("main p, [role='main'] p")
            p_texts = [ps.nth(i).inner_text().strip() for i in range(ps.count()) if ps.nth(i).inner_text().strip()]
            if p_texts:
                answer = "\n\n".join(p_texts)

        if answer:
            print("\n================== JAWABAN DARI CLAUDE ==================")
            print(answer)
            print("=========================================================\n")
            msg = f"🧠 JAWABAN CLAUDE\n\n{answer}"
            send_telegram_message(msg)
            send_chat_screenshot("02_claude_answer.png", "Tangkapan layar jawaban Claude")
        else:
            print("⚠️ Gagal mengekstrak teks otomatis, mengirim bukti tangkapan layar...")
            send_chat_screenshot("02_claude_answer.png", "⚠️ Claude merespons (detail ada di tangkapan layar).")

        context.storage_state(path=SESSION_FILE)
        browser.close()

if __name__ == "__main__":
    main()


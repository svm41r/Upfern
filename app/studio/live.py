"""Live sessions: record a stream into a rolling buffer, transcribe as it goes, let the AI pick clips."""
import collections
import json
import re
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import alerts, auth, brain, config, creators, ctx, jobs, load, media, past, transcribe, usage

SESSIONS: dict = {}
SEG_SEC = 6           # length of each buffer piece
PAD = 8               # seconds kept either side of a clip so it can be re-trimmed later
CHECK_EVERY = 180     # look at the stream every 3 minutes...
QUIET_MAX = 600       # ...but only ask the AI when something happened, or at most every 10 minutes when calm
WINDOW = 240          # seconds of transcript shown to the AI per check
SR = 16000
INGEST_STALL_SEC = 30


class NotLive(Exception):
    pass


def media_safe(e) -> str:
    from . import safe
    return safe.friendly(e, 300)


def normalize(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    m = re.match(r"^(https?://(?:www\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+))/?(?:[?#].*)?$", url)
    return m.group(1) + "/live" if m else url


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def resolve(url: str) -> dict:
    """Ask yt-dlp for the stream's playable address. Raises NotLive if the channel is offline."""
    import yt_dlp
    if media.is_youtube(url) and media.youtube_paused():
        raise NotLive(media.YT_BLOCKED)
    common = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _Quiet()}
    base = {**common, "format": "b[height<=720]/bv*[height<=720]+ba"}
    # Prefer single muxed streams (android_vr on YouTube Live has 720p60 muxed itag 300) so audio & video never split
    # onto separate HLS playlists with mismatched sequence clocks. Fall back to split streams only if no muxed exists.
    if media.is_youtube(url):
        mux_base = {**common, "format": "b[height<=720]"}
        tries = [
            dict(mux_base, extractor_args={"youtube": {"player_client": ["android_vr"]}}),
            dict(mux_base),
            *([dict(mux_base, **config.yt_cookies())] if config.yt_cookies() else []),
            *(dict(mux_base, extractor_args={"youtube": {"player_client": [c]}}) for c in ("web_safari", "tv", "mweb")),
            *([dict(base, **config.yt_cookies())] if config.yt_cookies() else []),
            dict(base),
            *(dict(base, extractor_args={"youtube": {"player_client": [c]}}) for c in ("android_vr", "tv", "web_safari", "mweb")),
        ]
    else:
        tries = [dict(base, **config.yt_cookies())]
    info, last = None, None
    for opts in tries:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                cand = ydl.extract_info(url, download=False)
            if cand and (not cand.get("is_live") or cand.get("requested_formats") or cand.get("url")):
                info = cand
                break
        except Exception as e:  # noqa: BLE001
            last = e
            if media.note_refusal(url, e):
                raise NotLive(media.YT_BLOCKED) from e
            low = str(e).lower()
            if "not currently live" in low or "is offline" in low or "will begin" in low:
                raise NotLive(str(e)[-200:]) from e
    if info is None:
        low = str(last).lower()
        from . import jsrt
        if media.is_youtube(url) and not jsrt.runtime():
            raise NotLive(jsrt.HOW)
        if any(k in low for k in ("no video formats", "requested format", "nsig", "signature", "unable to extract")):
            threading.Thread(target=media.update_ytdlp, daemon=True).start()
            raise NotLive("YouTube changed something, so ClipKar is updating its downloader now. Close ClipKar, "
                          "open it again in a minute, then start recording again.")
        raise NotLive(str(last)[-200:])
    if not info or not info.get("is_live"):
        raise NotLive("This channel is not live right now.")
    fmts = info.get("requested_formats") or [info]
    title = info.get("title") or ""
    if "(live)" in title and info.get("description"):  # Twitch puts the real stream title here
        title = str(info["description"]).splitlines()[0][:120]
    return {"urls": [f["url"] for f in fmts], "title": title,
            "id": info.get("id"), "began": info.get("release_timestamp") or info.get("timestamp"),
            "uploader": info.get("uploader") or info.get("channel") or ""}


def _online() -> bool:
    """Is the internet there at all? (A quick connection to two big DNS services.)"""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 443), timeout=3).close()
            return True
        except OSError:
            continue
    return False


def active():
    return [s for s in SESSIONS.values() if not s.finished and not s.user_stopped]


def active_mine():
    u = ctx.uid()
    return [s for s in active() if s.job["user"] == u]


def active_for(cid):
    return next((s for s in active_mine() if s.job["creator"] == cid), None)


def mine(jid):
    """The signed-in user's recording with this id, or None."""
    s = SESSIONS.get(jid)
    return s if s and s.job["user"] == ctx.uid() else None


class Session:
    def __init__(self, job, settings):
        self.job, self.settings = job, settings
        self.dir = Path(job["dir"])
        self.buf = self.dir / "buffer"
        self.buf.mkdir(exist_ok=True)
        (self.dir / "clips").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stop_flag = threading.Event()
        self.ended = False            # recording has stopped (stream over, stopped, or failed)
        self.finished = False         # everything is wrapped up
        self.end_note = "Stream ended"
        self.user_stopped = False     # Stop pressed: wrap up at once, no last AI check
        self.proc = None
        self.pending = bytearray()    # audio received but not yet transcribed
        self.clock_fix = 0.0          # how far the video-piece clock is behind the audio clock, last measured
        self.pending_base = 0         # sample index where `pending` starts
        self.samples = 0              # total audio samples received
        self.db = []                  # loudness per second
        self.words = []
        self.t_done = 0.0             # transcript is complete up to this second
        self.segments = []            # (path, start, end) on the session clock
        self.seg_clock = 0.0
        self.csv_seen = {}
        self.connection_clocks = {}
        self.part = 0
        self.chat = collections.Counter()   # second -> messages
        self.chat_total = 0
        self.chat_on = False
        self.last_check = 0.0
        self.words_at_check = 0
        self.checks = 0
        self.rejected = 0
        self.verdicts = []
        self.last_trigger = -999.0
        self.started_at = time.time()
        self.skipped = 0
        self.last_ai = 0.0
        self.tokens = 0
        self.manual = []
        self.n = 0
        self.spike = None             # a jump in loudness or chat waiting for its transcript
        self.said = None              # someone said "clip": that moment, waiting for its transcript
        self.last_review = 0.0         # stream second the last N-minute review read up to
        self.review_asked = False      # Review now was pressed
        self.reviews = 0
        self.status = "Listening to the stream"   # what the AI is doing right now, shown on the live card
        self.status_at = time.time()
        self.ai_busy = False                      # an AI check or review is running in the background
        self.last_said = -999.0
        self.last_spike = -999.0      # when the last jump was sent to the AI (stream seconds)
        self.base = {}                # running averages the jumps are measured against, shown on the card
        self.spikes = 0
        self.chat_tick = 0            # the last 10-second chat window that was looked at
        self.chat_clip_at = -999.0    # when chat last made a clip by itself
        self.chat_clips = 0

    # ---------------------------------------------------------------- recording
    def start(self):
        SESSIONS[self.job["id"]] = self
        threading.Thread(target=ctx.bind(self._ingest), daemon=True).start()
        threading.Thread(target=ctx.bind(self._analyze), daemon=True).start()
        m = re.match(r"^https?://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)", self.job["source"]["url"])
        if m:
            threading.Thread(target=ctx.bind(self._chat), args=(m.group(1).lower(),), daemon=True).start()

    def stop(self, note="Stopped by you"):
        """Instant for the user: the card and the creator show as stopped straight away. Clips already being made
        still finish in the background, but nothing new is transcribed or sent to the AI."""
        self.end_note = note
        self.user_stopped = True
        self.stop_flag.set()
        with self.lock:
            self.job.update(status="done", stage=note)
        jobs.save(self.job)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _ingest(self):
        fails, offline_since = 0, None
        url = self.job["source"]["url"]
        while not self.stop_flag.is_set():
            try:
                info = resolve(url)
            except NotLive as e:
                if self.samples and not _online():          # the internet dropped, not the stream: wait for it
                    self._say("📡 Internet dropped. Waiting for it to come back, the recording carries on after")
                    offline_since = offline_since or time.time()
                    if time.time() - offline_since < 600:
                        self.stop_flag.wait(10)
                        continue
                offline_since = None
                fails += 1
                if self.samples == 0 or fails >= 3:
                    if self.samples == 0:
                        self.job["error"] = f"Could not start recording: {e}"
                    break
                self.stop_flag.wait(10)
                continue
            if not self.job.get("uploader"):
                self.job.update(title=info["title"], uploader=info["uploader"])
            if not self.part and not self.samples:
                self.job["capture_origin"] = time.time()
                self.job["capture_media"] = {k: info[k] for k in ("id", "began")}
            captured_id = (self.job.get("capture_media") or {}).get("id")
            if captured_id and info.get("id") and captured_id != info["id"]:
                self.end_note = "Stream changed"
                break
            self.part += 1
            p = f"p{self.part:02d}"
            with self.lock:
                self.seg_clock = max(self.seg_clock, self.samples / SR)
                if info.get("began"):
                    self.connection_clocks[p] = time.time() - float(info["began"]) - self.seg_clock
            cmd = ["ffmpeg", "-v", "error", "-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_delay_max", "10"]
            for u in info["urls"]:
                cmd += ["-rw_timeout", "15000000", "-i", u]
            a_in = "1:a:0" if len(info["urls"]) > 1 else "0:a:0"
            cmd += ["-map", "0:v:0", "-map", a_in, "-c", "copy", "-f", "segment", "-segment_time", str(SEG_SEC),
                    "-reset_timestamps", "1", "-segment_format", "mpegts", "-segment_list", f"{p}.csv",
                    "-segment_list_type", "csv", f"{p}_%05d.ts",
                    "-map", a_in, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
            with open(self.dir / "ffmpeg.log", "ab") as errlog:
                self.proc = subprocess.Popen(cmd, cwd=self.buf, stdout=subprocess.PIPE, stderr=errlog,
                                             creationflags=media.NO_WINDOW)
                proc = self.proc
                last_data = time.monotonic()
                finished = threading.Event()

                def watch_ingest():
                    while not finished.wait(2):
                        if proc.poll() is not None:
                            return
                        if time.monotonic() - last_data >= INGEST_STALL_SEC:
                            self._say("Stream data stalled. Reconnecting…")
                            self.job["stage"] = "Reconnecting to the stream"
                            try:
                                proc.terminate()
                            except OSError:
                                pass
                            return

                watchdog = threading.Thread(target=watch_ingest, daemon=True)
                watchdog.start()
                got = 0
                try:
                    while True:
                        data = proc.stdout.read(SR * 2)  # one second of 16-bit mono
                        if not data:
                            break
                        last_data = time.monotonic()
                        if not got and not self.stop_flag.is_set():
                            self.job["stage"] = "Recording"
                        data = data[: len(data) // 2 * 2]
                        got += len(data)
                        x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                        level = float(20 * np.log10(np.sqrt((x ** 2).mean() + 1e-10))) if len(x) else -100.0
                        with self.lock:
                            self.pending += data
                            self.samples += len(data) // 2
                            self.db.append(level)
                    proc.wait()
                finally:
                    finished.set()
                    watchdog.join()
                    proc.stdout.close()
            self._scan_segments()
            if got <= SR * 2 * 20 and not _online():         # cut off by the internet: not counted as the stream ending
                self._say("📡 Internet dropped. Waiting for it to come back, the recording carries on after")
                self.stop_flag.wait(10)
                continue
            fails = 0 if got > SR * 2 * 20 else fails + 1
            if fails >= 3:
                break
            self.stop_flag.wait(3)
        self.ended = True

    def _scan_segments(self):
        """Pick up buffer pieces ffmpeg has finished writing."""
        with self.lock:
            for i in range(1, self.part + 1):
                f = self.buf / f"p{i:02d}.csv"
                if not f.exists():
                    continue
                rows = [r.split(",") for r in f.read_text(encoding="utf-8", errors="ignore").splitlines() if r.count(",") >= 2]
                for name, a, b in (r[:3] for r in rows[self.csv_seen.get(i, 0):]):
                    try:
                        dur = max(0.0, float(b) - float(a))
                    except ValueError:
                        continue
                    self.segments.append((self.buf / name, self.seg_clock, self.seg_clock + dur))
                    self.seg_clock += dur
                self.csv_seen[i] = len(rows)
            keep_from = self.seg_clock - self.settings["buffer_min"] * 60
            while self.segments and self.segments[0][2] < keep_from:
                self.segments.pop(0)[0].unlink(missing_ok=True)

    # ---------------------------------------------------------------- chat (Twitch only)
    def _chat(self, channel):
        while not self.stop_flag.is_set() and not self.ended:
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
                s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
                s.sendall(f"NICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{channel}\r\n".encode())
                s.settimeout(30)
                rest = b""
                self.chat_on = True
                while not self.stop_flag.is_set() and not self.ended:
                    try:
                        data = s.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    *lines, rest = (rest + data).split(b"\r\n")
                    for line in lines:
                        if line.startswith(b"PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif b" PRIVMSG #" in line:
                            with self.lock:
                                self.chat[int(self.samples / SR)] += 1
                                self.chat_total += 1
                s.close()
            except Exception:  # noqa: BLE001
                pass
            self.chat_on = False
            self.stop_flag.wait(5)

    def _chat_spike(self, s, e):
        """True if chat was at least 3x busier than usual during (and just after) these seconds."""
        elapsed = self.samples / SR
        if not self.chat_total or elapsed < 60:
            return False
        avg = self.chat_total / elapsed
        a, b = int(s), int(e) + 6
        rate = sum(self.chat.get(t, 0) for t in range(a, b + 1)) / (b - a + 1)
        return rate > max(3 * avg, 1.0)

    # ---------------------------------------------------------------- analysis
    def _transcribe_step(self):
        with self.lock:
            n = len(self.pending) // 2
            hurry = self.ended or bool(self.manual)
            if n < (SR if hurry else SR * 7):   # about every 7 s, so "what was just said" really is live
                return False
            audio = np.frombuffer(bytes(self.pending[: n * 2]), dtype=np.int16)
            base = self.pending_base
        cut = n
        if not hurry or n > SR * 24:
            # cut at the quietest moment between 4 and 6.5 s, so no word is split; the rest waits for the next pass
            _, cut = next(transcribe._chunks(audio, 4.0, 6.5))
        words = transcribe.transcribe(audio[:cut])
        off = base / SR
        for w in words:
            w["s"], w["e"] = round(w["s"] + off, 2), round(w["e"] + off, 2)
        with self.lock:
            del self.pending[: cut * 2]
            self.pending_base += cut
            self.words += words
            self.t_done = self.pending_base / SR
        self._triggers(words)
        (self.dir / "words.json").write_text(json.dumps(self.words), encoding="utf-8")
        return True

    CHAT_WINDOW, CHAT_RATIO, CHAT_MIN, CHAT_BASE, CHAT_LEN, CHAT_BEFORE, CHAT_GAP = 10, 3.0, 20, 18, 30, 15, 60

    def _chat_watch(self):
        """The chat watcher: count chat in 10-second windows; a window at 3 times the normal one (the median of the
        last three minutes, and at least 20 messages) is a moment. The 30 seconds around it become a clip straight
        away, with no AI in between. Chat reacts a few seconds late, so most of the clip is before the spike."""
        if not self.settings.get("chat_clips", True) or not (self.chat_on or self.chat_total) or self.user_stopped:
            return
        w = self.CHAT_WINDOW
        with self.lock:
            now = int(self.samples / SR)
            tick = now // w - 1                      # the last window that is complete
            if tick <= self.chat_tick or tick < 6:
                return
            self.chat_tick = tick
            counts = [sum(self.chat.get(t, 0) for t in range(k * w, k * w + w)) for k in range(max(0, tick - self.CHAT_BASE), tick + 1)]
        n, before = counts[-1], sorted(counts[:-1])
        if len(before) < 6:
            return
        normal = before[len(before) // 2]
        end_of_window = (tick + 1) * w
        if n < max(self.CHAT_MIN, self.CHAT_RATIO * max(normal, 1)) or end_of_window - self.chat_clip_at < self.CHAT_GAP:
            return
        self.chat_clip_at = end_of_window
        self.chat_clips += 1
        start = max(0.0, end_of_window - w - self.CHAT_BEFORE)
        self.manual.append({"start": start, "end": start + self.CHAT_LEN,
                            "why": f"Chat exploded: {n} messages in {w} seconds, against a normal {normal}"})

    AUTO_BEFORE, AUTO_AFTER, AUTO_GAP = 30, 20, 60   # seconds of stream sent around a jump; least time between jumps
    JUMP = 0.6                                        # 60% louder or busier than the last three minutes

    def _watch_spikes(self):
        """Always on. Keep a running average of loudness and chat over the last three minutes (leaving out the
        last few seconds), and mark a moment when the last five seconds jump a set percentage above it. Nothing is
        sent yet: the AI gets that moment once its transcript is ready, with the half minute before it."""
        with self.lock:
            n = len(self.db)
            if n < 60:
                self.base = {"warming": True}
                return
            db = np.array(self.db[max(0, n - 190):], dtype=np.float32)
            chat = [self.chat.get(t, 0) for t in range(max(0, n - 190), n)] if (self.chat_on or self.chat_total) else None
        amp = 10 ** (db / 20)                       # loudness as plain level, so a percentage means something
        base, cur = float(amp[:-10].mean()), float(amp[-5:].mean())
        loud_jump = cur / max(base, 1e-6) - 1
        pct = self.JUMP
        why = []
        if loud_jump >= pct:
            why.append(f"loudness +{round(loud_jump * 100)}%")
        chat_base = chat_now = None
        if chat is not None:
            chat_base = sum(chat[:-10]) / max(1, len(chat) - 10)
            chat_now = sum(chat[-8:]) / 8          # chat is bursty, so look at a slightly longer stretch
            if chat_now >= max(1.5, chat_base * (1 + pct)):
                why.append(f"chat +{round((chat_now / max(chat_base, 0.01) - 1) * 100)}%")
        self.base = {"loud_db": round(20 * np.log10(max(base, 1e-6)), 1), "loud_now": round(loud_jump * 100),
                     "chat": round(chat_base, 2) if chat_base is not None else None,
                     "chat_now": round(chat_now, 2) if chat_now is not None else None, "pct": round(pct * 100)}
        if why and not self.spike and n - self.last_spike >= self.AUTO_GAP:
            self.spike = {"t": float(n - 3), "why": ", ".join(why)}
            self.last_spike = n
            self.spikes += 1

    def _ai_check(self, window=None, why=None):
        """Judge 1 reads only the words. If it finds a moment, judge 2 reads only the loudness and chat activity
        for the same stretch, without seeing the words. A clip is made when both say yes (or as set in Setup)."""
        st = self.settings
        every = int(st.get("ai_every_min", 3)) or 3
        if window:                    # just the stretch around a jump, or around someone saying "clip"
            ws, now = max(0.0, window[0]), window[1]
        else:
            now = self.t_done
            ws = max(0.0, now - max(every, 3) * 60 - 90)  # each check overlaps the last by 90 s, so nothing falls between
        self.last_check = self.t_done
        win = [w for w in self.words if ws <= w["s"] <= now]
        recent = [c for c in self.job["clips"] if c.get("score", 0) > 0 and time.time() - c.get("made_at", 0) < 3600]
        last_clip = max((c.get("made_at", 0) for c in self.job["clips"]), default=self.started_at)
        must = st["must_clip_min"] > 0 and time.time() - last_clip >= st["must_clip_min"] * 60
        verdict = {"t": round(now), "transcript": None, "reaction": None, "trigger": why}
        if len(win) < 8:
            self.skipped += 1
            return self._verdict(verdict, "Skipped: nobody talking")
        self._say("🔎 " + (f"Checking a moment ({why})" if why else "Quick check of the last few minutes"))
        if len(recent) >= st["live_per_hour"] and not must:
            self.skipped += 1
            return self._verdict(verdict, f"Skipped: {st['live_per_hour']} clips this hour already")
        usage.set_context(self.job["creator"], self.job["title"] or "Live stream")
        title, up = self.job["title"], self.job["uploader"]
        try:
            cands, tok = brain.judge_transcript(win, ws, now, title, up, st["min_len"], st["max_len"], self.job["clips"],
                                                n=1 if every <= 2 else 2, must=must, focus=self.job.get("focus", ""))
        except Exception as e:  # noqa: BLE001
            self.job["ai"]["last_error"] = str(e)[-200:]
            return self._verdict(verdict, "AI error: " + str(e)[-80:])
        self.checks += 1
        self.tokens += tok
        keep = [c for c in cands if c["score"] >= (40 if must else st["min_score"])]
        verdict["transcript"] = max((c["score"] for c in cands), default=0)
        if not keep:
            return self._verdict(verdict, "Words judge: nothing clip-worthy")
        z = brain.loud_zscore(np.array(self.db, dtype=np.float32))
        lo, hi = int(ws), int(now) + 1
        chat = [self.chat.get(t, 0) for t in range(lo, hi)] if (self.chat_on or self.chat_total) else None
        try:
            sig, tok2 = brain.judge_signals(z[lo:hi], chat, lo, now)
            self.checks += 1
            self.tokens += tok2
        except Exception as e:  # noqa: BLE001
            sig = []
            self.job["ai"]["last_error"] = str(e)[-200:]
        added = 0
        for c in keep:
            match = [m for m in sig if m["end"] >= c["start"] - 10 and m["start"] <= c["end"] + 10 and m["score"] >= 50]
            rs = max((m["score"] for m in match), default=0)
            verdict["reaction"] = rs
            both = bool(match)
            ok = both or (st["decide_mode"] == "either" and c["score"] >= 90) or (must and added == 0)
            if not ok:
                self.rejected += 1
                self._verdict(verdict, "Reaction judge said no, not clipped")
                continue
            if any(abs(c["start"] - k["start"]) < st["min_gap_min"] * 60 for k in self.job["clips"]):
                self._verdict(verdict, "Too close to the last clip")
                continue
            why = "Both judges agreed" if both else ("Words judge was very sure" if c["score"] >= 90 and
                                                     st["decide_mode"] == "either" else "Forced: no clip for a while")
            c["judges"] = {"words": c["score"], "reaction": rs or None, "why": why}
            c["reason"] = f"{c['reason']} ({why.lower()}.)"
            self._add_clip(c)
            self._verdict(verdict, "Clipped: " + why.lower())
            added += 1
            must = False
            if added >= (1 if every <= 2 else 2):
                break

    def review_now(self):
        self.review_asked = True
        self._say("🧠 Review asked: the AI starts reading as soon as it's free" if self.ai_busy else "🧠 Starting the review now")

    def _review(self, why="Review"):
        """Every N minutes: the AI reads the whole stretch since the last review and makes its best clips itself.
        No second judge and no hourly cap: this is the steady supply of clips while the stream runs."""
        st = self.settings
        mass = bool(st.get("mass"))
        span = (5 if mass else max(2, int(st.get("review_min", 10) or 10))) * 60
        now = self.t_done
        ws = max(0.0, max(self.last_review - 30, now - span - 30))      # a little overlap so nothing falls between
        self.last_review = now
        self.reviews += 1
        win = [w for w in list(self.words) if ws <= w["s"] <= now]
        verdict = {"t": round(now), "transcript": None, "reaction": None, "trigger": why}
        self._say(f"🧠 Reading the last {max(1, round((now - ws) / 60))} min of the stream to find the best clips")
        if len(win) < 25:
            self.skipped += 1
            return self._verdict(verdict, f"{why}: too little talking to clip")
        usage.set_context(self.job["creator"], self.job["title"] or "Live stream")
        n = 4 if mass else max(1, min(4, int(st.get("review_clips", 2) or 2)))
        try:
            cands, tok = brain.judge_transcript(win, ws, now, self.job["title"], self.job["uploader"], st["min_len"], st["max_len"],
                                                self.job["clips"], n=n, must=True, focus=self.job.get("focus", ""))
        except Exception as e:  # noqa: BLE001
            self.job["ai"]["last_error"] = str(e)[-200:]
            self._say("⚠️ The AI didn't answer (" + ("daily limit reached on every model" if "per day" in str(e).lower() else str(e)[-60:]) + ")")
            return self._verdict(verdict, "AI error: " + str(e)[-80:])
        self.checks += 1
        self.tokens += tok
        verdict["transcript"] = max((c["score"] for c in cands), default=0)
        good = [c for c in cands if c["score"] >= 45]
        self._say(f"✂️ Found {len(good)} good moment{'s' if len(good) != 1 else ''}, making the clip{'s' if len(good) != 1 else ''}" if good
                  else "🔎 Nothing good enough in that stretch, still watching")
        added = 0
        for c in sorted(cands, key=lambda c: -c["score"]):
            if added >= n or c["score"] < (30 if mass else 45):
                break
            if any(abs(c["start"] - k["start"]) < 20 for k in self.job["clips"]):
                continue
            c["judges"] = {"words": c["score"], "reaction": None, "why": why}
            c["reason"] = f"{c['reason']} ({why.lower()}: best of the last {round((now - ws) / 60)} min.)"
            self._add_clip(c)
            added += 1
        self._verdict(verdict, f"{why}: made {added} clip{'s' if added != 1 else ''}" if added else f"{why}: nothing good enough")

    def _say(self, text):
        self.status, self.status_at = text, time.time()
        try:
            self._publish()
        except Exception:  # noqa: BLE001
            pass

    def _bg(self, fn, *a):
        """Run an AI check or review on its own thread, so listening and transcribing never wait for the AI."""
        self.ai_busy = True

        def run():
            try:
                fn(*a)
            except Exception as e:  # noqa: BLE001
                self.job["ai"]["last_error"] = str(e)[-200:]
            finally:
                self.ai_busy = False
                if not self.ended and self.status.startswith(("🧠", "🔎")):   # results ("Found 2...", "Nothing good...") stay up
                    self._say("Listening to the stream")
        threading.Thread(target=ctx.bind(run), daemon=True, name="live-ai").start()

    def _verdict(self, v, result):
        self.verdicts.append(dict(v, result=result))
        del self.verdicts[:-6]

    CLIP_WORDS = re.compile(r"\bclip(\s+(it|that|this))?\b", re.I)
    SAID_BEFORE, SAID_AFTER, SAID_GAP = 60, 15, 45

    def _triggers(self, new_words):
        """Built in: when someone says "clip" ("clip it", "clip that"), the AI looks at that moment as soon as its
        transcript is ready; people say it right after something worth clipping. Then the creator's own trigger
        phrases from Setup, which clip straight away with no AI check."""
        for w in new_words or []:
            if self.CLIP_WORDS.search(re.sub(r"[^a-z' ]", "", w["w"].lower())) and w["s"] - self.last_said > self.SAID_GAP \
                    and not self.said:
                self.said = {"t": w["s"], "why": 'Someone said "clip"'}
                self.last_said = w["s"]
        phrases = [p.strip().lower() for p in self.settings.get("trigger_words", "").split(",") if p.strip()]
        if not phrases or not new_words:
            return
        key = [re.sub(r"[^a-z0-9']", "", w["w"].lower()) for w in new_words]
        for p in phrases:
            parts = [re.sub(r"[^a-z0-9']", "", x) for x in p.split()]
            for i in range(len(key) - len(parts) + 1):
                if key[i:i + len(parts)] == parts:
                    t = new_words[i]["s"]
                    if t - self.last_trigger > 45:
                        self.last_trigger = t
                        self.manual.append({"start": max(0.0, t - 30), "end": t + 2, "why": f'Someone said "{p}"'})

    def _manual_step(self):
        for req in [m for m in self.manual if m["end"] <= self.t_done or self.ended]:
            self.manual.remove(req)
            text = " ".join(w["w"] for w in self.words if req["start"] <= w["s"] <= req["end"])
            usage.set_context(self.job["creator"], self.job["title"] or "Live stream")
            c = brain.describe(text, self.job["title"], self.job["uploader"])
            c.update(start=round(req["start"], 2), end=round(min(req["end"], self.samples / SR), 2), score=0)
            if req.get("why"):
                c["reason"] = req["why"]
            self._add_clip(c)

    def _add_clip(self, c):
        self.n += 1
        c.update(id=f"clip{self.n:03d}", style=self.settings["style"], layout=self.settings["layout"],
                 cap_lang=self.job.get("options", {}).get("cap_lang") or self.settings.get("cap_lang") or "roman",
                 status="queued", version=0, made_at=time.time())
        with self.lock:
            self.job["clips"].append(c)
        jobs.save(self.job)
        jobs.submit_live(lambda: self._render(c))

    def _render(self, clip):
        """Runs on the render worker: copy the needed buffer pieces, then make the clip."""
        clip["stage"] = "Waiting for the stream to reach the end of this moment"
        jobs.save(self.job)
        deadline = time.time() + 45
        while time.time() < deadline:
            self._scan_segments()
            if self.ended or (self.segments and self.segments[-1][2] + self.clock_fix >= clip["end"] - .05):
                break
            time.sleep(1)
        with self.lock:
            segs = [s for s in self.segments if s[2] + self.clock_fix > clip["start"] - PAD and s[1] + self.clock_fix < clip["end"] + PAD and s[0].exists()]
        chains = []
        for segment in segs:
            if not chains or segment[1] - chains[-1][-1][2] > .25:
                chains.append([])
            chains[-1].append(segment)
        segs = next((chain for chain in chains
                     if chain[0][1] + self.clock_fix <= clip["start"] + .25
                     and chain[-1][2] + self.clock_fix >= clip["end"] - .25), [])
        if not segs:
            clip.update(status="error", error="The recording did not capture the full selected moment. Choose a newer buffered moment.")
            clip.pop("stage", None)
            jobs.save(self.job)
            return
        if "width" not in self.job:
            meta = media.probe(segs[0][0])
            self.job.update(width=meta["width"], height=meta["height"])
        clip["stage"] = "Cutting it out of the recording"
        lst = self.dir / "clips" / f"{clip['id']}.txt"
        lst.write_text("".join(f"file '{s[0].as_posix()}'\n" for s in segs), encoding="utf-8")
        src = self.dir / "clips" / f"{clip['id']}.src.ts"
        try:
            media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                       "-c", "copy", str(src)])
        except Exception as e:  # noqa: BLE001
            clip.update(status="error", error=media_safe(e))
            jobs.save(self.job)
            return
        finally:
            lst.unlink(missing_ok=True)
        dur = segs[-1][2] - segs[0][1]
        offset = self._align(src, segs[0][1] + self.clock_fix, dur)
        self.clock_fix = offset - segs[0][1]  # the two clocks drift ~4 s an hour; start the next clip from here
        clip["stage"] = "Fetching the selected moment in 1080p"
        jobs.save(self.job)
        try:
            if not media.has_1080p(media.probe(src)):
                reference = src
                try:
                    part = segs[0][0].stem.split("_", 1)[0]
                    clock = self.connection_clocks.get(part)
                    src, offset, dur = self._hd_source(clip, reference, offset,
                                                       clock=None if clock is None else clock - self.clock_fix)
                finally:
                    reference.unlink(missing_ok=True)
        except Exception as e:  # the 720p recording never goes to the renderer
            clip.update(status="error", error=f"1080p source unavailable. {media_safe(e)}")
            clip.pop("stage", None)
            jobs.save(self.job)
            return
        clip.update(src=str(src), src_offset=round(offset, 2), src_end=round(offset + dur, 2))
        clip["start"] = max(clip["start"], clip["src_offset"])
        clip["end"] = min(clip["end"], clip["src_end"])
        clip["stage"] = "Adding captions and rendering"
        jobs.render_one(self.job, clip, list(self.words))
        clip.pop("stage", None)

    def _hd_source(self, clip, reference, reference_offset, *, clock=None):
        """Get a native HD DVR window and verify its time against the recording's audio."""
        identity = self.job.get("capture_media") or {}
        url = self.job["source"]["url"]
        began = identity.get("began")
        origin = self.job.get("capture_origin")
        archive = past.find(url) if media.is_twitch(url) else None
        if archive and not began:
            vod = media._replay_info(archive["url"])
            began = vod.get("timestamp") or vod.get("release_timestamp")
        if not began or not origin:
            raise ValueError("The platform did not provide the stream start time.")
        if clock is None:
            clock = origin - float(began)
        now = time.time() - float(began)
        want = max(0.0, clock + clip["start"] - PAD - 45)
        till = min(now - 10, clock + clip["end"] + PAD)
        if till - want < 3:
            raise ValueError("This moment is not available in the archive yet.")
        directory = self.dir / "clips"
        stem = f"{clip['id']}.hd.src"
        if media.is_youtube(url) and identity.get("id"):
            video_url = f"https://www.youtube.com/watch?v={identity['id']}"
            src = past.youtube_section(video_url, directory, stem, want, till, 1080,
                                       require_hd=True, expected_id=identity["id"])
        elif archive:
            till = min(till, archive["seconds"])
            if till - want < 3:
                raise ValueError("The Twitch VOD does not cover this moment yet.")
            src = media.download_section(archive["url"], directory, stem, want, till, 1080, require_hd=True)
        else:
            raise ValueError("This platform did not expose a downloadable 1080p rewind range.")
        accepted = False
        low_wav, hd_wav = directory / f"{stem}.ref.wav", directory / f"{stem}.hd.wav"
        try:
            meta = media.probe(src)
            if not media.has_1080p(meta):
                raise ValueError("The archive returned a lower-resolution source.")
            media.extract_audio(reference, low_wav)
            media.extract_audio(src, hd_wav)
            low, high = media.load_audio(low_wav), media.load_audio(hd_wav)
            offsets = []
            for anchor in (clip["start"], (clip["start"] + clip["end"]) / 2, clip["end"] - 2):
                at = max(0, round((anchor - reference_offset) * SR))
                anchor = reference_offset + at / SR
                match = media.match_audio(high, low[at:at + 2 * SR], anchor + clock - want, search=52)
                if match is not None:
                    offsets.append(anchor - match)
            if len(offsets) < 2 or max(offsets) - min(offsets) > .15:
                raise ValueError("The 1080p archive could not be aligned to the recorded moment.")
            offset = float(np.median(offsets))
            if offset > clip["start"] + .1 or offset + meta["duration"] < clip["end"] - .1:
                raise ValueError("The 1080p archive did not cover the whole clip.")
            accepted = True
            return src, offset, meta["duration"]
        finally:
            low_wav.unlink(missing_ok=True)
            hd_wav.unlink(missing_ok=True)
            if not accepted:
                src.unlink(missing_ok=True)

    def _align(self, src, guess, dur):
        """Where `src` really starts on the transcript clock. The video pieces and the audio feed keep separate
        clocks that drift apart, so transcribe the clip's own audio and line its words up with the live words.
        Local speech model only: no AI calls."""
        key = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())
        try:
            wav = src.with_suffix(".wav")
            media.extract_audio(src, wav)
            heard = transcribe.transcribe(media.load_audio(wav))
            wav.unlink(missing_ok=True)
            with self.lock:
                near = [w for w in self.words if guess - 15 <= w["s"] <= guess + dur + 15]
            diffs = []
            for h in heard:
                k = key(h["w"])
                if len(k) < 3:
                    continue
                m = [w["s"] for w in near if key(w["w"]) == k and abs(w["s"] - (guess + h["s"])) < 8]
                if m:
                    diffs.append(min(m, key=lambda s: abs(s - (guess + h["s"]))) - h["s"])
            if len(diffs) >= 4:
                return float(np.median(diffs))
        except Exception:  # noqa: BLE001 - fall back to the piece clock
            pass
        return guess

    def clip_last(self, seconds):
        self._scan_segments()
        with self.lock:
            if not self.segments or not self.samples:
                raise ValueError("Waiting for the first completed video segment.")
            end = min(self.samples / SR, self.segments[-1][2] + self.clock_fix)
            start = end - seconds
            # A reconnect can leave a gap. Clip only the continuous tail.
            for i in range(len(self.segments) - 1, -1, -1):
                a = self.segments[i][1] + self.clock_fix
                if i == 0 or self.segments[i][1] - self.segments[i - 1][2] > .25:
                    start = max(start, a, 0)
                    break
                if a <= start:
                    break
            if end - start < 5:
                raise ValueError("Waiting for five seconds of continuous video in the buffer.")
            self.manual.append({"start": start, "end": end})

    def _publish(self):
        elapsed = self.samples / SR
        lines = brain.make_lines([w for w in self.words if w["s"] >= self.t_done - 45])[-5:]
        with self.lock:
            sec = int(elapsed)
            self.job.update(
                duration=elapsed,
                live={"elapsed": round(elapsed), "buffer_sec": round(self.segments[-1][2] - self.segments[0][1]) if self.segments else 0,
                      "lines": [l["text"] for l in lines], "level": [round(x) for x in self.db[-60:]],
                      "chat": [self.chat.get(t, 0) for t in range(max(0, sec - 60), sec)] if self.chat_on or self.chat_total else None,
                      "checks": self.checks, "skipped": self.skipped, "tokens": self.tokens, "words": len(self.words),
                      "rejected": self.rejected, "verdicts": list(reversed(self.verdicts)),
                      "every_min": int(self.settings.get("ai_every_min", 3)) or 3,
                      "auto": None,
                      "pending_clips": len(self.manual),
                      "status": self.status, "status_for": round(time.time() - self.status_at), "ai_busy": self.ai_busy,
                      "behind": max(0, round(elapsed - self.t_done)),
                      "making": [{"title": c.get("title") or c.get("hook") or c["id"], "stage": c.get("stage") or "In line to render"}
                                 for c in self.job["clips"] if c.get("status") in ("queued", "rendering")][:4],
                      "review_min": int(self.settings.get("review_min", 10) or 0), "reviews": self.reviews,
                      "next_review": max(0, round(int(self.settings.get("review_min", 10) or 0) * 60 - (self.t_done - self.last_review)))
                      if int(self.settings.get("review_min", 10) or 0) else None})
        jobs.save(self.job)

    def _analyze(self):
        cid = self.job["creator"]
        started = False
        while True:
            busy = False
            try:
                if not self.user_stopped and not self.ended:      # a recording cannot run for ever
                    if time.time() - self.started_at > config.MAX_LIVE_HOURS * 3600:
                        self.stop(f"Stopped after {config.MAX_LIVE_HOURS:g} hours, the longest one recording can run")
                self._scan_segments()
                busy = self._transcribe_step()
                if not started and self.samples:
                    started = True
                    self.job.update(status="live", stage="Recording")
                    creators.log(cid, f"Recording started: {self.job['title']}")
                self._chat_watch()
                self._manual_step()
                # Three ways the AI is asked: on the creator's schedule (every N minutes), and always, when the
                # stream suddenly gets louder or chat busier, or when someone says "clip".
                every = int(self.settings.get("ai_every_min", 3)) or 3
                self._watch_spikes()
                sp, said = self.spike, self.said
                rmin = int(self.settings.get("review_min", 10) or 0)
                if self.user_stopped or self.ai_busy:
                    pass
                elif self.review_asked:
                    self.review_asked = False
                    self._bg(self._review, "Review now")
                elif (rmin or self.settings.get("mass")) and self.t_done - self.last_review >= (5 if self.settings.get("mass") else rmin) * 60:
                    self._bg(self._review, f"{rmin}-min review")
                elif rmin and self.ended and not busy and self.t_done - self.last_review >= 90:
                    self._bg(self._review, "End-of-stream review")
                elif said and (self.t_done >= said["t"] + self.SAID_AFTER or (self.ended and not busy)):
                    self.said = None
                    self._bg(self._ai_check, (said["t"] - self.SAID_BEFORE, said["t"] + self.SAID_AFTER), said["why"])
                elif sp and (self.t_done >= sp["t"] + self.AUTO_AFTER or (self.ended and not busy)):
                    self.spike = None
                    self._bg(self._ai_check, (sp["t"] - self.AUTO_BEFORE, sp["t"] + self.AUTO_AFTER), "Jump: " + sp["why"])
                elif self.t_done - self.last_check >= every * 60 or (self.ended and not busy and self.t_done > self.last_check):
                    self.last_check = self.t_done
                    self._bg(self._ai_check)
                self._publish()
            except Exception as e:  # noqa: BLE001
                self.job["ai"]["last_error"] = str(e)[-200:]
            if self.ai_busy:
                pass                      # let a running AI check finish before closing up
            elif (self.user_stopped and not self.manual) or (self.ended and not busy and len(self.pending) < SR * 2 and not self.manual):
                break
            if not busy:
                time.sleep(2)
        failed = bool(self.job.get("error")) and not started
        self.job.update(status="error" if failed else "done", stage="Could not record" if failed else self.end_note)
        jobs.save(self.job)
        creators.log(cid, f"Recording finished ({self.end_note.lower()}): {len(self.job['clips'])} clips, "
                          f"{round(self.samples / SR / 60)} min" if not failed else f"Recording failed: {self.job['error']}")

        if failed:
            alerts.notify(f"recording failed for {self.job['source']['name']}: {self.job['error']}")
        else:
            alerts.notify(f"recording finished for {self.job['source']['name']}: {len(self.job['clips'])} clips, "
                          f"{round(self.samples / SR / 60)} min.")

        def cleanup():  # runs after any queued renders, then frees the buffer
            for f in self.buf.glob("*"):
                f.unlink(missing_ok=True)
            self.finished = True
            SESSIONS.pop(self.job["id"], None)

        jobs.submit_live(cleanup)


def recover(job):
    """After a restart: finish the clips of an interrupted recording from the stream pieces still on disk."""
    c = creators.get(job["creator"]) or dict(creators.DEFAULTS)
    s = Session(job, c)
    s.ended, s.end_note = True, "Recording stopped when the studio closed"
    s.part = len(list(s.buf.glob("p*.csv")))
    s.seg_clock = 0.0
    s.csv_seen = {}
    with s.lock:
        for i in range(1, s.part + 1):
            f = s.buf / f"p{i:02d}.csv"
            rows = [r.split(",") for r in f.read_text(encoding="utf-8", errors="ignore").splitlines() if r.count(",") >= 2]
            for name, a, b in (r[:3] for r in rows):
                try:
                    dur = max(0.0, float(b) - float(a))
                except ValueError:
                    continue
                s.segments.append((s.buf / name, s.seg_clock, s.seg_clock + dur))
                s.seg_clock += dur
            s.csv_seen[i] = len(rows)
    try:
        s.words = json.loads((s.dir / "words.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        s.words = []
    s.n = len(job["clips"])
    todo = [k for k in job["clips"] if k.get("status") == "queued"]
    for k in todo:
        jobs.submit_live(lambda k=k: s._render(k))

    def cleanup():
        for f in s.buf.glob("*"):
            f.unlink(missing_ok=True)
        job.pop("recover", None)
        jobs.save(job)
        creators.log(job["creator"], f"Finished {len(todo)} clip(s) from a recording interrupted by a restart")

    jobs.submit_live(cleanup)


def catch_up_now(jid):
    """The live card's "Clip what I missed" button."""
    s = mine(jid)
    if not s or s.ended:
        raise ValueError("That stream isn't recording.")
    c = creators.get(s.job["creator"])
    cj = past.start_catchup(c, s.job["source"]["url"], min_seconds=60)
    if not cj:
        raise ValueError("The stream only just started: there's nothing before the recording to clip.")
    s.job["catchup"] = cj["id"]
    jobs.save(s.job)
    return cj


def start_session(creator: dict, url=None, options=None) -> dict:
    url = normalize(url or creator.get("channel") or "")
    if not re.match(r"^https?://[^/]+\.[^/]+/", url + "/"):
        raise ValueError("Add this creator's channel link first.")
    if active_for(creator["id"]):
        raise ValueError("This creator is already being recorded.")
    if not media.site_allowed(url):
        raise ValueError("Channel links must be from YouTube, Twitch or Kick.")
    if media.is_youtube(url) and media.youtube_paused():
        raise ValueError(media.YT_BLOCKED.replace("download videos", "record YouTube streams")
                         .replace("Upload the video file instead, or use a Twitch or Kick link. YouTube links",
                                  "Twitch and Kick streams still work. YouTube"))
    if len(active_mine()) >= config.MAX_LIVE_PER_USER:
        raise ValueError(f"You're already recording {config.MAX_LIVE_PER_USER} streams, the most at once. Stop one first.")
    if len(active()) >= config.MAX_LIVE_TOTAL or (len(active()) >= 2 and load.busy()):
        raise ValueError("ClipKar is recording as many streams as it can right now. Try again in a little while.")
    job = jobs.create({"type": "live", "url": url, "name": f"{creator['name']} live"}, options or {}, enqueue=False,
                      creator=creator["id"], kind="live")
    job.update(status="live", stage="Connecting to the stream")
    jobs.save(job)
    Session(job, creator).start()
    if creator.get("catch_up", True):
        def catch():
            try:
                cj = past.start_catchup(creator, url)
                if cj:
                    job["catchup"] = cj["id"]
                    jobs.save(job)
            except Exception as e:  # noqa: BLE001 - the live recording goes on either way
                creators.log(creator["id"], f"Couldn't clip the earlier part of the stream: {str(e)[-160:]}")
        threading.Thread(target=ctx.bind(catch), daemon=True).start()
    return job


def _watch_user(uid):
    """Start recording any of this user's auto-record creators who have gone live."""
    from . import profiles
    with ctx.as_user(uid):
        try:
            todo = [c for c in creators.all_creators() if c["auto_watch"] and c["channel"]]
            if not todo:
                return
        except Exception:  # noqa: BLE001
            return
        for c in todo:
            _watch_one(c)


def check_now(cid):
    """Auto-record was just turned on: look now, on a thread, instead of waiting for the next round."""
    uid = ctx.uid()

    def run():
        from . import profiles
        with ctx.as_user(uid):
            c = creators.get(cid)
            if not c or not c["auto_watch"] or not c["channel"]:
                return
            for url in c.get("channels", {}).values():
                profiles._live.pop(url.rstrip("/").lower(), None)     # a fresh answer, not one from two minutes ago
            _watch_one(c)
            profiles.refresh(cid, force=True)
    threading.Thread(target=run, daemon=True).start()


def _watch_one(c):
    """Start recording this auto-record creator if they are live on any of their channels."""
    from . import profiles
    try:
        if active_for(c["id"]) or len(active_mine()) >= config.MAX_LIVE_PER_USER or len(active()) >= config.MAX_LIVE_TOTAL or (len(active()) >= 2 and load.busy()):
            return
        # the chosen channel first, then their other platforms
        urls = [c["channel"]] + [u for u in c.get("channels", {}).values() if u != c["channel"]]
        for url in urls:
            # one shared "are they live?" answer per channel, however many people follow it
            if not profiles.live_shared(profiles.platform_of(url), url).get("live"):
                continue
            if media.is_youtube(url):
                # check YouTube will hand over the stream before making a recording that could only fail
                if media.youtube_paused():
                    continue
                try:
                    resolve(normalize(url))
                except NotLive:
                    continue
            start_session(c, url)
            where = creators.platform_key(url).title()
            creators.log(c["id"], f"Went live on {where}: recording started automatically")
            alerts.notify(f"{c['name']} went live on {where}, recording started.")
            break
    except NotLive:
        pass
    except Exception as e:  # noqa: BLE001
        creators.log(c["id"], f"Auto-record check failed: {str(e)[-150:]}")


def watch_loop():
    """Every two minutes, start recording any creator set to auto-record who has gone live. Covers every user,
    several at a time, so the round stays short however many accounts there are."""
    from concurrent.futures import ThreadPoolExecutor
    time.sleep(20)
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="watch")
    while True:
        t0 = time.time()
        try:
            list(pool.map(_watch_user, auth.user_ids()))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(15, 120 - (time.time() - t0)))


def _resume_recordings(paused):
    """Recordings that were running when the server stopped: start recording the same channel again, if the
    stream is still on. Tried for a few minutes, since the server may come back a little before the network."""
    time.sleep(10)
    for _ in range(6):
        left = []
        for uid, cid, url in paused:
            with ctx.as_user(uid):
                try:
                    c = creators.get(cid)
                    if not c or active_for(cid):
                        continue
                    resolve(normalize(url))      # raises NotLive if the stream has ended: no empty recording is made
                    start_session(c, url)
                    creators.log(cid, "Recording picked up again after a server update")
                except NotLive:
                    continue                  # the stream ended while the server was away
                except Exception:  # noqa: BLE001 - try again shortly
                    left.append((uid, cid, url))
        if not left:
            return
        paused = left
        time.sleep(30)


def start():
    paused = []
    for j in jobs.all_jobs():
        if j.get("recover"):
            with ctx.as_user(j["user"]):
                recover(j)
        if j.get("was_live") and j.get("creator"):
            paused.append((j["user"], j["creator"], j["source"]["url"]))
            j.pop("was_live", None)
            jobs.save(j)
    if paused:
        threading.Thread(target=_resume_recordings, args=(paused,), daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()

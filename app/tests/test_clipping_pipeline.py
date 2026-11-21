"""Comprehensive regression tests for Upfern audio track verification, language integrity,
whole-video clip discovery, deduplication, and multi-creator generalization (MrBeast, IShowSpeed, NDL)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from studio import brain, config, ctx, hear, jobs, media, roman, server  # noqa: E402


class TestAudioTrackVerification(unittest.TestCase):
    def _mrbeast_raw_formats(self):
        """Simulate yt-dlp raw process=False + DASH formats where Arabic is index 0 and format_id 234 collides."""
        return [
            # Arabic first in raw list (root cause of original bug)
            {"format_id": "234", "vcodec": "none", "acodec": "mp4a.40.2", "language": "ar",
             "format_note": "العربية - dubbed", "language_preference": -1, "protocol": "m3u8_native",
             "abr": None, "url": "https://manifest.googlevideo.com/ar234.m3u8"},
            {"format_id": "140-0", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "language": "ar",
             "format_note": "Arabic, low", "language_preference": -1, "protocol": "https", "abr": 128,
             "url": "https://rr.googlevideo.com/ar140"},
            {"format_id": "234", "vcodec": "none", "acodec": "mp4a.40.2", "language": "es",
             "format_note": "Español - dubbed", "language_preference": -1, "protocol": "m3u8_native",
             "abr": None, "url": "https://manifest.googlevideo.com/es234.m3u8"},
            # English original appears last
            {"format_id": "234", "vcodec": "none", "acodec": "mp4a.40.2", "language": "en",
             "format_note": "English - original (original)", "language_preference": 10, "protocol": "m3u8_native",
             "abr": None, "url": "https://manifest.googlevideo.com/en234.m3u8"},
            {"format_id": "140-12", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "language": "en",
             "format_note": "English original, low", "language_preference": 10, "protocol": "https", "abr": 128,
             "url": "https://rr.googlevideo.com/en140"},
            {"format_id": "312", "vcodec": "avc1.64002a", "acodec": "none", "width": 1920, "height": 1080,
             "fps": 60, "protocol": "m3u8_native", "url": "https://manifest.googlevideo.com/v1080.m3u8"},
        ]

    def test_mrbeast_defaults_to_english_original_and_rejects_arabic(self):
        info = {"language": "en", "formats": self._mrbeast_raw_formats()}
        insp = media.inspect_audio_tracks(info)
        self.assertTrue(insp["verified"])
        self.assertEqual(insp["original_lang"], "en")
        self.assertTrue(insp["multi_track"])
        # DASH + HLS per language consolidate cleanly (en:original, ar:dubbed, es:dubbed)
        self.assertEqual(len(insp["tracks"]), 3)
        ids = {t["id"] for t in insp["tracks"]}
        self.assertEqual(ids, {"en:original", "ar:dubbed", "es:dubbed"})

        sel_def = media.select_audio_track(info, "")
        self.assertEqual(sel_def["lang"], "en")
        self.assertFalse(sel_def["is_dubbed"])

        filtered = media.filter_audio_formats(info["formats"], sel_def)
        self.assertTrue(all(media.base_lang(f.get("language")) == "en" for f in filtered))
        picked_hls = media._pick_best_audio_format([f for f in filtered if f["protocol"] == "m3u8_native"], hls=True)
        self.assertEqual(picked_hls["language"], "en")
        self.assertIn("original", picked_hls["format_note"].lower())

        # Explicit Arabic dub request selects Arabic only when explicitly requested
        sel_ar = media.select_audio_track(info, "ar:dubbed")
        self.assertEqual(sel_ar["lang"], "ar")
        self.assertTrue(sel_ar["is_dubbed"])
        filtered_ar = media.filter_audio_formats(info["formats"], sel_ar)
        self.assertEqual(len(filtered_ar), 2)
        self.assertTrue(all(media.base_lang(f.get("language")) == "ar" for f in filtered_ar))

    def test_unverified_multitrack_raises_and_returns_422_with_tracks(self):
        ambiguous_info = {
            "language": "",
            "formats": [
                {"format_id": "234-0", "vcodec": "none", "acodec": "mp4a.40.2", "language": "ar",
                 "format_note": "Arabic", "language_preference": None, "protocol": "m3u8_native"},
                {"format_id": "234-1", "vcodec": "none", "acodec": "mp4a.40.2", "language": "en",
                 "format_note": "English", "language_preference": None, "protocol": "m3u8_native"},
            ],
        }
        insp = media.inspect_audio_tracks(ambiguous_info)
        self.assertFalse(insp["verified"])
        with self.assertRaises(media.UnverifiedAudioTrackError) as cm:
            media.select_audio_track(ambiguous_info, "")
        self.assertEqual(len(cm.exception.tracks), 2)
        self.assertIn("ar:standard", cm.exception.message if hasattr(cm.exception, "message") else str(cm.exception))

        # Explicit track selection resolves the ambiguity cleanly
        sel = media.select_audio_track(ambiguous_info, "en:standard")
        self.assertEqual(sel["lang"], "en")

        # Real FastAPI route POST /api/jobs enforces 422 before creating a job
        from fastapi.testclient import TestClient

        fake_preview = {
            "title": "Ambiguous Multi-Track Video",
            "uploader": "Creator",
            "duration": 600.0,
            "is_live": False,
            "thumb": "",
            "language": "",
            "audio_tracks": insp["tracks"],
            "original_audio_verified": False,
            "original_audio_lang": "",
            "original_audio_label": "",
            "multi_track": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            u_dir = tmp_path / "u_test"
            (u_dir / "jobs").mkdir(parents=True)
            test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            client = TestClient(server.app, base_url="http://localhost")
            with mock.patch.dict(media._PREVIEWS, {test_url: fake_preview}, clear=False), \
                 mock.patch.object(server, "_need_verified", return_value=None), \
                 mock.patch.object(server, "_quota", return_value=None), \
                 mock.patch.object(server, "_throttle", return_value=None), \
                 mock.patch.object(server, "_job_slots", return_value=None), \
                 mock.patch.object(config, "user_dir", return_value=u_dir), \
                 mock.patch.object(ctx, "uid", return_value="u_test"), \
                 mock.patch.object(media, "check_youtube", return_value=None), \
                 mock.patch.object(jobs, "enqueue", return_value=None), \
                 mock.patch.object(jobs._q, "put", return_value=None):
                r422 = client.post("/api/jobs", json={"url": test_url, "options": {"max_clips": 18}})
                self.assertEqual(r422.status_code, 422)
                body = r422.json()
                self.assertEqual(body["code"], "unverified_audio_track")
                self.assertEqual(len(body["tracks"]), 2)

                r200 = client.post("/api/jobs", json={"url": test_url, "options": {"audio_track": "en:standard", "max_clips": 15}})
                self.assertEqual(r200.status_code, 200)
                j = r200.json()
                self.assertEqual(j["audio_lang"], "en")
                self.assertEqual(j["audio_track"]["id"], "en:standard")
                jobs.delete(j["id"])


class TestCaptionLanguageAndScripts(unittest.TestCase):
    def test_caption_matches_audio_lang_and_youtube_words(self):
        ar_words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3}
                    for i, w in enumerate(["مرحبا", "بكم", "في", "هذه", "التجربة", "المجنونة", "اليوم", "يا", "شباب", "هيا", "بنا", "نبدأ"])]
        en_words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3}
                    for i, w in enumerate(["Welcome", "everyone", "to", "one", "hundred", "hours", "inside", "Grand", "Theft", "Auto", "Six", "today"])]

        self.assertFalse(media.caption_matches_audio_lang(ar_words, "en"))
        self.assertTrue(media.caption_matches_audio_lang(en_words, "en"))
        self.assertFalse(media.caption_matches_audio_lang(en_words, "ar"))
        self.assertTrue(media.caption_matches_audio_lang(ar_words, "ar"))

        # Dubbed track must never borrow original captions
        self.assertIsNone(media.youtube_words("https://www.youtube.com/watch?v=ymCHX_Dcc-U", audio_lang="ar", is_dubbed=True))

        # Mock yt-dlp response with Arabic-only subtitle when English audio was chosen -> must reject
        class _FakeYdl:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {
                    "language": "en",
                    "subtitles": {},
                    "automatic_captions": {"ar": [{"ext": "json3", "url": "https://example.com/ar.json3"}]},
                }

            def urlopen(self, url):
                class _R:
                    def __init__(self, b):
                        self._b = b

                    def read(self, n=-1):
                        return self._b
                return _R(self._payload)

        ar_json3 = json.dumps({
            "events": [{"tStartMs": 0, "dDurationMs": 5000,
                        "segs": [{"utf8": "مرحبا بكم في هذه التجربة المجنونة اليوم يا شباب هيا بنا نبدأ الآن"}]}]
        }).encode("utf-8")
        with mock.patch.object(media, "_ydl", return_value=_FakeYdl(ar_json3)):
            res = media.youtube_words("https://www.youtube.com/watch?v=ymCHX_Dcc-U", lang="", audio_lang="en", is_dubbed=False)
        self.assertIsNone(res)

        # Clean up zero-width spaces and duplicate repaint words
        dirty_json3 = {
            "events": [
                {"tStartMs": 1000, "dDurationMs": 900,
                 "segs": [{"utf8": "yo\u200b", "tOffsetMs": 0}, {"utf8": " bro", "tOffsetMs": 200}]},
                {"tStartMs": 1000, "dDurationMs": 900,
                 "segs": [{"utf8": "yo\u200b", "tOffsetMs": 10}, {"utf8": "bro", "tOffsetMs": 210}]},
            ]
        }
        cleaned = media._caption_words(dirty_json3)
        self.assertEqual([w["w"] for w in cleaned], ["yo", "bro"])

    def test_no_silent_translation_and_multilingual_preservation(self):
        ar_words = [
            {"w": "والله", "s": 0.2, "e": 0.6},
            {"w": "هذا", "s": 0.6, "e": 0.9},
            {"w": "جنون", "s": 0.9, "e": 1.3},
            {"w": "كامل", "s": 1.3, "e": 1.8},
        ]
        # roman.should_romanize must NEVER touch Arabic!
        self.assertFalse(roman.should_romanize(ar_words, "ar"))
        self.assertTrue(roman.should_romanize([{"w": "क्या", "s": 0.1, "e": 0.4}], "en"))

        with mock.patch.object(brain, "_fix_caption_words", side_effect=lambda ws, *a, **k: ws), \
             mock.patch.object(brain, "translate_caption_words", side_effect=AssertionError("Must not translate silently")):
            kept = brain.english_captions(ar_words, "ar", "Arabic Original", translate=False)
        self.assertEqual([w["w"] for w in kept], ["والله", "هذا", "جنون", "كامل"])

        with mock.patch.object(brain, "translate_caption_words",
                               return_value=[{"w": "Wallah", "s": 0.2, "e": 0.6}, {"w": "this", "s": 0.6, "e": 0.9},
                                             {"w": "is", "s": 0.9, "e": 1.3}, {"w": "insane", "s": 1.3, "e": 1.8}]) as m_tr:
            tr = brain.english_captions(ar_words, "ar", "Arabic Original", translate=True)
        m_tr.assert_called_once()
        self.assertEqual([w["w"] for w in tr], ["Wallah", "this", "is", "insane"])

        # Multilingual code-switching detection & preservation
        spanglish = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3}
                     for i, w in enumerate(["Bro", "vamos", "porque", "esto", "esta", "crazy", "hermano", "look", "at", "that"])]
        d_sp = brain.detect_language_info(spanglish, audio_lang="en", whisper_lang="en")
        self.assertTrue(d_sp["multilingual"])
        self.assertEqual(d_sp["code"], "en+es")

        plain_en = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25}
                    for i, w in enumerate("No no no way bro the car just exploded to pieces!".split())]
        d_plain = brain.detect_language_info(plain_en, audio_lang="en", whisper_lang="en")
        self.assertEqual(d_plain["code"], "en")
        self.assertFalse(d_plain["multilingual"])

        hinglish = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3}
                    for i, w in enumerate(["Bhai", "kya", "scene", "hai", "bro", "look", "at", "this", "website", "yaar"])]
        d_hi = brain.detect_language_info(hinglish, audio_lang="en", whisper_lang="")
        self.assertEqual(d_hi["code"], "hi+en")


class TestDiscoveryAndCreators(unittest.TestCase):
    def _make_words(self, segments):
        words = []
        for s, e, txt in segments:
            toks = txt.split()
            step = (e - s) / max(1, len(toks))
            for idx, tok in enumerate(toks):
                words.append({"w": tok, "s": round(s + idx * step, 2), "e": round(s + (idx + 1) * step, 2)})
        return words

    def test_30min_discovery_balanced_coverage_dedup_and_shortfall(self):
        # 24 strong moments across beginning (0-600), middle (600-1200), end (1200-1800) + duplicates
        topics = [
            "helicopter landing on Maze Bank roof nearly explodes",
            "Karl steals the jet ski during police chase",
            "Chandler hides inside a garbage truck from hunters",
            "Nolan offers a bribe for the golden briefcase",
            "Tareq launches the buggy across the broken bridge",
            "Mack falls off the crane platform at sunset",
            "Secret bunker vault opens with laser trap inside",
            "Submarine dives under the aircraft carrier undetected",
            "Monster truck crushes three sports cars in seconds",
            "Parachute fails to open until fifty feet above water",
            "Tank blocks the tunnel ambush at midnight",
            "Secret tunnel shortcut discovered behind waterfall",
            "Bounty hunter team surrounds the mansion courtyard",
            "Speedboat race flips through burning barrel obstacle",
            "Forklift drops the million dollar crate onto docks",
            "Sniper shot pops the getaway van front tire",
            "Train tracks standoff forces last second jump",
            "Casino rooftop deal collapses into total chaos",
            "Night vision stealth mission trips silent alarm",
            "Cargo plane ramp opens midair above desert",
            "Monster yacht party ambush shocks everyone aboard",
            "Secret code unlocks underground garage hypercar",
            "Final countdown betrayal leaves everyone stunned",
            "Last survivor wins the grand prize trophy",
        ]
        segs = []
        cands = []
        for idx, topic in enumerate(topics):
            start = idx * 72.0 + 10.0
            end = start + 24.0
            segs.append((start, end, topic))
            cands.append({
                "start": start, "end": end, "score": 82 - (idx % 5),
                "hook_score": 4, "clarity_score": 4, "payoff_score": 4,
                "category": "shock", "reason": f"Standout moment: {topic[:50]}.",
                "title": f"Moment {idx}: {topic[:40]}", "hook": f"{topic[:55].title()} 😱",
                "critic": {"overall": 8, "hook": 4, "payoff": 4, "clarity": 4, "duplicate_of": None, "note": "Strong moment"},
            })
        # Add 4 overlapping / same-joke duplicates with slightly lower scores
        for base_idx in (1, 8, 16, 20):
            s = base_idx * 72.0 + 16.0
            e = s + 22.0
            cands.append({
                "start": s, "end": e, "score": 70,
                "hook_score": 3, "clarity_score": 4, "payoff_score": 3,
                "category": "shock", "reason": f"Repeat of {topics[base_idx][:40]}.",
                "critic": {"overall": 7, "hook": 3, "payoff": 4, "clarity": 4, "duplicate_of": None, "note": "Repeat"},
            })

        words = self._make_words(segs)
        winners, stats = brain.select_best_clips(cands, words, duration=1800.0, want=18, quality_threshold=56.0)
        self.assertEqual(len(winners), 18)
        self.assertGreaterEqual(stats["deduplicated_out"], 4)
        self.assertEqual(stats["coverage"]["beginning"] + stats["coverage"]["middle"] + stats["coverage"]["end"], 18)
        self.assertGreaterEqual(stats["coverage"]["beginning"], 3)
        self.assertGreaterEqual(stats["coverage"]["middle"], 3)
        self.assertGreaterEqual(stats["coverage"]["end"], 3)
        self.assertEqual(stats["shortfall_reason"], "")
        # Verify zero overlap among selected clips
        for a, b in zip(winners, winners[1:]):
            self.assertLessEqual(a["end"], b["start"] + 0.01)

        # Low-density sparse 30-minute video: only 7 strong + 10 weak -> return 7 and explain shortfall
        sparse_cands = cands[:7] + [
            {"start": 600.0 + k * 90.0, "end": 620.0 + k * 90.0, "score": 40,
             "hook_score": 2, "clarity_score": 2, "payoff_score": 2,
             "category": "other", "reason": f"Calm filler segment {k} with distinct topic alpha{k}.",
             "critic": {"overall": 3, "hook": 2, "payoff": 1, "clarity": 2, "duplicate_of": None, "note": "Weak"}}
            for k in range(10)
        ]
        sparse_win, sparse_stats = brain.select_best_clips(sparse_cands, words, duration=1800.0, want=18, quality_threshold=56.0)
        self.assertEqual(len(sparse_win), 7)
        self.assertEqual(sparse_stats["weak_filtered"], 10)
        self.assertIn("Found 7 unique clip-worthy moments", sparse_stats["shortfall_reason"])

    def test_ishowspeed_reaction_tail_room_and_ndl_group_banter(self):
        # 1) Speed-style audio: quiet idle 0-3s, spoken setup+scream 3-18s, continued loud laugh 18-20.5s
        sr = 16000
        samples = np.zeros(30 * sr, dtype=np.int16)
        samples[3 * sr:18 * sr] = 2500
        samples[14 * sr:21 * sr] = 22000  # explosive reaction continuing 3s after last word at 18.0s
        feats = media.audio_features_per_second(samples)
        self.assertGreater(float(np.max(feats["spike"])), 7.0)
        z = brain.loud_zscore(feats["db"])
        words_speed = self._make_words([
            (3.2, 6.0, "Chat wait wait look at this right now!"),
            (13.5, 18.0, "No way Ronaldo just did that what?!"),
        ])
        lines_speed = brain.make_lines(words_speed, z, spike=feats["spike"])
        s, e = brain._snap({"start": 0.5, "end": 18.0}, lines_speed, words_speed, 10, 45, 30.0, z=z)
        self.assertGreaterEqual(s, 2.5)   # dead air 0-3s trimmed
        self.assertGreaterEqual(e, 19.5)  # tail extended to keep explosive reaction after final word at 18.0s

        # 2) NDL-style multi-speaker group banter + UK slang + code-switching
        ndl_words = self._make_words([
            (1.0, 5.0, "Wallahi bruv the mandem are completely finished innit"),
            (5.1, 9.2, "Wait hold on Niko what did Chunkz just say right there"),
            (9.3, 14.0, "Fam that was bare peak stop capping on camera"),
            (14.1, 18.5, "No everybody saw that replay yaar come on!"),
        ])
        # streamer_only must skip group/challenge shows and preserve co-hosts
        with mock.patch.object(brain, "ask_ai", side_effect=AssertionError("Should not filter non-gaming group show")):
            preserved = hear.streamer_only(ndl_words, who="NDL", title="NDL Try Not To Laugh Challenge With The Boys")
        self.assertEqual(len(preserved), len(ndl_words))

        # Slang words like wallahi/bruv/mandem must NOT trigger Arabic classification or AI alteration
        d_ndl = brain.detect_language_info(ndl_words, audio_lang="en", whisper_lang="en")
        self.assertEqual(d_ndl["code"], "en")
        with mock.patch.object(brain, "ask_ai",
                               return_value=([w["w"] if w["w"].lower() != "wallahi" else "swear" for w in ndl_words], "m", 10)):
            fixed = brain.fix_caption_words(ndl_words, title="NDL Banter", lang="en")
        self.assertTrue(any(w["w"] == "Wallahi" for w in fixed))


    def test_iso639_2_normalization(self):
        self.assertEqual(media.base_lang("eng"), "en")
        self.assertEqual(media.base_lang("ara"), "ar")
        self.assertEqual(media.base_lang("hin"), "hi")
        self.assertEqual(media.lang_name("eng"), "English")
        self.assertEqual(media.lang_name("ara"), "Arabic")

    def test_zero_overlap_boundary_and_partial_window_fallback(self):
        # Two adjacent distinct moments with 0.85s boundary bleed must BOTH survive with zero overlap
        words = self._make_words([
            (122.0, 139.0, "He climbs onto the skyscraper ledge and drops the briefcase into the fountain"),
            (138.4, 158.8, "Suddenly a shark leaps out of the aquarium tank and breaks the glass floor"),
        ])
        cands = [
            {"start": 122.0, "end": 139.2, "score": 82, "hook_score": 4, "clarity_score": 4, "payoff_score": 4,
             "category": "shock", "reason": "Briefcase drops from rooftop ledge into fountain.",
             "critic": {"overall": 8, "hook": 4, "payoff": 4, "clarity": 4, "duplicate_of": None, "note": "Good"}},
            {"start": 138.35, "end": 158.9, "score": 80, "hook_score": 4, "clarity_score": 4, "payoff_score": 4,
             "category": "shock", "reason": "Shark shatters aquarium glass right afterward.",
             "critic": {"overall": 8, "hook": 4, "payoff": 4, "clarity": 4, "duplicate_of": None, "note": "Good"}},
        ]
        win, _ = brain.select_best_clips(cands, words, duration=300.0, want=5, quality_threshold=56.0)
        self.assertEqual(len(win), 2)
        self.assertLessEqual(win[0]["end"], win[1]["start"])

        # Partial-window failure: Window 0 succeeds via AI, Window 1 raises -> Window 1 still gets backup picks
        seg_words = self._make_words([
            (20.0 + i * 10.0, 29.0 + i * 10.0, f"unique comedy moment alfa{i} happens right here with strong payoff")
            for i in range(48)
        ])
        db = np.full(520, -25.0, dtype=np.float32)
        db[360:390] = -4.0  # clear loud peak in window 1 (255..520s)
        ai_win0 = [[{"start": 20.0, "end": 42.0, "score": 84, "hook_score": 4, "clarity_score": 4, "payoff_score": 4,
                     "category": "funny", "reason": "Opening joke lands cleanly right away."}]]
        with mock.patch.object(brain, "ask_ai", side_effect=[
            (ai_win0[0], "qwen/qwen3.8-27b", 500),
            RuntimeError("rate limit"),
            RuntimeError("rate limit"),
        ]):
            clips, meta = brain.find_clips(seg_words, db, 520.0, title="Test", uploader="Creator", max_clips=6, lean=True)
        self.assertEqual(meta["model"], "qwen/qwen3.8-27b")
        self.assertTrue(any(c["start"] >= 250.0 for c in clips), f"Expected backup moment in second window, got {clips}")

    def test_whisper_prompt_guard_and_sparse_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            dummy_flac = td_path / "sample.flac"
            dummy_flac.write_bytes(b"fLaC\x00\x00\x00\x22")
            posts = []

            class _Resp:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"language": "hi", "words": []}

            def fake_post(url, **kwargs):
                posts.append(dict(kwargs.get("data") or {}))
                return _Resp()

            with mock.patch.object(hear.ai, "groq_key", return_value="gsk_test"), \
                 mock.patch.object(hear.httpx, "post", side_effect=fake_post):
                hear._whisper_post(dummy_flac, title="THIS GIRL IS SOO CORNY", lang="", use_prompt=True)
                hear._whisper_post(dummy_flac, title="THIS GIRL IS SOO CORNY", lang="en", use_prompt=True)
                hear._whisper_post(dummy_flac, title="THIS GIRL IS SOO CORNY", lang="en", use_prompt=False)

            self.assertNotIn("prompt", posts[0])
            self.assertNotIn("CORNY", posts[1].get("prompt", ""))
            self.assertNotIn("prompt", posts[2])

            dummy_src = td_path / "src.mp4"
            dummy_src.write_bytes(b"fake")

            # Case A: English prompted call returns 2 words, unprompted retry returns 18 -> adopts 18
            en_18 = [{"word": f"word{i}", "start": 0.5 + i * 0.4, "end": 0.8 + i * 0.4} for i in range(18)]
            with mock.patch.object(hear.subprocess, "run"), \
                 mock.patch.object(hear, "_whisper_post", side_effect=[
                     {"language": "en", "words": en_18[:2]},
                     {"language": "en", "words": en_18},
                 ]) as pmock:
                out_words, det = hear.clip_words(dummy_src, 10.0, 25.0, 0.0, title="CLICKBAIT", lang="en", expected_lang="en")
            self.assertEqual(len(out_words), 18)
            self.assertEqual(det, "en")
            self.assertEqual(pmock.call_count, 2)
            self.assertFalse(pmock.call_args_list[1].kwargs.get("use_prompt", True))

            # Case B: Auto-detect already returns >= sparse_floor code-switched words -> only 1 call
            hi_mix = [{"word": w, "start": 0.5 + i * 0.4, "end": 0.8 + i * 0.4}
                      for i, w in enumerate(["मुझे", "समझ", "नहीं", "I'm", "confused", "यार", "come", "on", "भाई", "seriously", "what"])]
            with mock.patch.object(hear.subprocess, "run"), \
                 mock.patch.object(hear, "_whisper_post", return_value={"language": "hi", "words": hi_mix}) as pmock_hi:
                out_hi, det_hi = hear.clip_words(dummy_src, 10.0, 25.0, 0.0, title="CLICKBAIT", lang="", expected_lang="hi")
            self.assertEqual(len(out_hi), 11)
            self.assertEqual(det_hi, "hi")
            self.assertEqual(pmock_hi.call_count, 1)

            # Case C: streamer_only must reject drops leaving < 50% of words
            w_3_lines = []
            for idx, txt in enumerate([
                "alpha beta gamma delta epsilon zeta eta theta iota kappa",
                "ok bro",
                "wow yes",
            ]):
                for j, tok in enumerate(txt.split()):
                    t = idx * 4.0 + j * 0.25
                    w_3_lines.append({"w": tok, "s": t, "e": t + 0.2})
            with mock.patch.object(brain, "ask_ai", return_value=({"game": [0]}, "m", 10)):
                kept = hear.streamer_only(w_3_lines, who="Creator", title="GTA Gameplay")
            self.assertEqual(len(kept), len(w_3_lines))

            # Case D: render_one must not let 3 Whisper words overwrite a 20-word source transcript
            job_dir = td_path / "job"
            (job_dir / "clips").mkdir(parents=True)
            words_20 = [{"w": f"spoken{i}", "s": 10.5 + i * 0.5, "e": 10.8 + i * 0.5} for i in range(20)]
            job = {
                "id": "j_test", "dir": str(job_dir), "source_path": str(dummy_src), "duration": 100.0,
                "width": 1920, "height": 1080, "title": "THIS GIRL IS SOO CORNY", "uploader": "MrJayPlays",
                "creator": "", "lang": "hi", "audio_lang": "hi", "options": {"lang": "auto", "cap_lang": "roman"},
            }
            clip = {"id": "clip01", "start": 10.0, "end": 22.0, "layout": "blur", "score": 80}
            hallucinated_3 = [{"w": "I", "s": 11.0, "e": 11.2}, {"w": "LOVE", "s": 11.3, "e": 11.5}, {"w": "YOU", "s": 11.6, "e": 11.9}]
            with mock.patch.object(jobs.media, "probe", return_value={"width": 1920, "height": 1080, "audio_streams": [{"index": 0, "lang": "hi"}]}), \
                 mock.patch.object(jobs.media, "has_1080p", return_value=True), \
                 mock.patch.object(jobs, "auto_fill"), \
                 mock.patch.object(jobs, "follow"), \
                 mock.patch.object(jobs, "save"), \
                 mock.patch.object(jobs.config, "ai_ready", return_value=False), \
                 mock.patch.object(jobs.config, "outro", return_value=(None, False)), \
                 mock.patch.object(jobs.creators, "log"), \
                 mock.patch.object(hear, "ready", return_value=True), \
                 mock.patch.object(hear, "clip_words", return_value=(hallucinated_3, "en")), \
                 mock.patch.object(jobs.render, "render_clip", return_value={"file": "clip01.mp4", "width": 1080, "height": 1920}) as rmock:
                jobs.render_one(job, clip, words_20)
            self.assertNotEqual(clip.get("heard_by"), "whisper")
            self.assertEqual(len(clip.get("fixed_words") or []), 20)
            self.assertEqual(len(rmock.call_args.args[4]), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)

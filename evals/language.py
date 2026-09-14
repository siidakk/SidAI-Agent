import asyncio, re, sys
sys.path.insert(0, r"C:\Users\Malika\Axon")
from backend import llm

CASES = [
    ("plain English",          "What is the capital of France?",            "en"),
    ("English, Indian name",   "Who is Sourav Joshi?",                      "en"),
    ("English, Hindi song",    "What does the song Tum Hi Ho mean?",        "en"),
    ("English, one Hindi word","Can you do a quick jugaad for this?",       "en"),
    ("real Hindi (Roman)",     "Mujhe aaj ka mausam bata do",               "hi"),
    ("real Hindi (Devanagari)","आज का मौसम कैसा है?",                        "hi"),
]

DEV = re.compile(r"[\u0900-\u097F]")
HI_WORDS = re.compile(r"\b(hai|hain|kya|nahi|aap|mujhe|karo|raha|rahi|ka|ki|ke|"
                      r"mein|aur|bata|kar|se|ko|yeh|woh|hoga|gaya)\b", re.I)

def guess(text):
    if DEV.search(text): return "hi"
    words = re.findall(r"[A-Za-z']+", text)
    if not words: return "?"
    hits = len(HI_WORDS.findall(text))
    return "hi" if hits >= 3 or hits / max(len(words), 1) > 0.18 else "en"

async def main():
    ok = 0
    for name, ask, want in CASES:
        out = ""
        async for ev in llm.stream_reply([{"role": "user", "content": ask}]):
            if isinstance(ev, dict) and ev.get("type") in ("token", "text", "delta"):
                out += str(ev.get("text") or ev.get("content") or "")
            elif isinstance(ev, str):
                out += ev
        got = guess(out)
        mark = "pass" if got == want else "FAIL"
        ok += got == want
        print(f"  {mark}  {name:26} want {want}  got {got}   {out[:70]!r}")
    print(f"\n{ok}/{len(CASES)} correct")

asyncio.run(main())

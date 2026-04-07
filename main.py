import subprocess
import time

stream_url = "https://www.twitch.tv/beardhero"

print("🎧 Starting stable audio-only stream from BeardHero...")
print("   This version prioritizes reliable audio over ultra-low latency.\n")

try:
    # Streamlink - stable settings
    stream_process = subprocess.Popen([
        "streamlink",
        "--loglevel", "none",
        "--stdout",
        "--twitch-disable-ads",
        "--stream-segment-threads", "2",
        stream_url,
        "audio_only"
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    # ffplay - clean & stable audio settings (works better on Chrome OS Linux)
    play_process = subprocess.Popen([
        "ffplay",
        "-nodisp",                    # no video window
        "-autoexit",
        "-f", "mpegts",
        "-af", "volume=2.0",          # increase if too quiet (try 1.5 → 3.0)
        "-fflags", "nobuffer",        # reduce some buffering issues
        "-flags", "low_delay",
        "-"
    ], stdin=stream_process.stdout, stderr=subprocess.DEVNULL)

    print("✅ Stream started. You should hear audio clearly now.")
    print("   Press Ctrl+C to stop.\n")

    # Keep the script running
    while True:
        time.sleep(10)

except KeyboardInterrupt:
    print("\n\n🛑 Stopped.")
    if 'stream_process' in locals():
        stream_process.terminate()
    if 'play_process' in locals():
        play_process.terminate()

except Exception as e:
    print(f"Error: {e}")

Tiny media fixtures generated with ffmpeg, so local files can be exercised without network access:
`tone.wav` (0.5 s, 8 kHz mono), `slide.png` (64x36) and `clip.mp4` (1 s, 160x90 @ 25 fps, H.264 + AAC).
The samples `tests/test_prepare.py` and the live test need beyond these are generated at test time.

    ffmpeg -f lavfi -i sine=frequency=440:sample_rate=8000:duration=0.5 tone.wav
    ffmpeg -f lavfi -i testsrc2=size=64x36 -frames:v 1 slide.png
    ffmpeg -f lavfi -i testsrc2=size=160x90:rate=25:duration=1 -f lavfi -i sine=sample_rate=8000:duration=1 \
      -c:v libx264 -c:a aac -shortest clip.mp4

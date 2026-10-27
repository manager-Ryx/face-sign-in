# Animated guides

Short looping animations shown in the app's how-to windows
(`presence_monitor/ui/guides.py`). Until a guide's file is here, the window
shows a placeholder with the steps instead.

| File | Shown in | What it shows |
|---|---|---|
| `hello-only.webp` (or `.gif`) | "Let Face Unlock appear on your lock screen" – from setup's *Get ready* step and the Overview, when Windows only allows Windows Hello sign-in | Settings → Accounts → Sign-in options → *Additional settings* → turning off "For improved security, only allow Windows Hello sign-in for Microsoft accounts on this device (Recommended)" |

## Making one

- **Format:** animated WebP (preferred: small and sharp) or GIF. It loops.
- **Size:** 16:9. Make it 1120 × 630, so it stays crisp when shown at 560 × 315
  on high-DPI screens. Keep the file under about 5 MB.
- **Length:** about 8–15 seconds. Highlight each click (a circle or a pulse) and
  pause briefly on the final switch turning off.
- **Privacy:** record with a demo account, or cover the name, email address and
  picture in Settings. Nothing personal goes in this public repository.
- **Both themes:** the window can be light or dark. A recording in Windows'
  dark mode, framed with a little padding, looks fine on both.

To convert a screen recording (for example with ffmpeg):

```powershell
ffmpeg -i recording.mp4 -vf "fps=15,scale=1120:-1:flags=lanczos" -loop 0 -quality 80 hello-only.webp
```

To preview the window without changing any setting:
`python -m tools.ui_preview out\`, which writes `*-guide-hello-only.png`.

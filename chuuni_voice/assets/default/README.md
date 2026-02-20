# Default Character Assets

Place your audio files here. Supported formats: `.mp3`, `.wav`, `.ogg`, `.aiff`

## File naming

Files must be named after the Claude Code hook event (lowercase):

| Filename | Fires when… |
|---|---|
| `pretooluse.mp3` | Claude is about to run a tool |
| `posttooluse.mp3` | A tool call just finished |
| `notification.mp3` | Claude sends a notification |
| `stop.mp3` | The session ends |

## Example layout

```
~/.config/chuuni/characters/default/
  pretooluse.mp3
  posttooluse.wav
  notification.mp3
  stop.mp3
```

> Tip: short clips (0.5–2 s) work best to avoid overlap with the next event.

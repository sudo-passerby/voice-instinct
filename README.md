# voice-instinct

A voice-first personal agent that runs on your own machine and handles everyday errands, in the
spirit of [Instinct](https://techcrunch.com/2026/09/28/viral-ai-agent-instinct-raises-1b-series-c-at-a-10b-valuation/),
the texting agent. You talk to it, it does the legwork, and it asks you by voice only when it needs a
decision or a choice. Local and private, open source, bring your own model.
Not affiliated with Instinct.

## What it does

- It lives in the background. Say "Nova" (the name is configurable) and a floating orb appears, listens, thinks
  and speaks, then hides again.
- You just talk to it. Speech-to-text (Nemotron streaming, Parakeet or Whisper) and text-to-speech (Kokoro) run locally,
  around a model of your choice: Gemini by default, or any OpenAI-compatible endpoint or Ollama.
- Optional, with a supported GPU: `qwen3-asr-gpu` (Qwen3-ASR-1.7B) is the most accurate English speech-to-text and gets names you list right, at about 10 GB of disk, about 7.5 GB of RAM while running and about 0.45 s per command (see `[stt]` in `config.example.toml`).
- It hands errands to your own logged-in `claude` CLI as a tracked background task, runs it
  autonomously in a sandbox, and tells you the result.
- When the task needs a go-ahead or an answer, it asks you aloud and carries on in the same run.
- It remembers you. "Remember that I'm vegetarian" applies at once, and "forget that" removes it. Every
  night it files the day's conversations into plain markdown in `~/.voice-instinct/memory/` (a git repo),
  one file per person, place or preference, and keeps `profile.md`, a short summary of you that every
  conversation and errand starts with. It starts out knowing your country from your timezone. You can read
  and edit all of it. Installed before this? Re-run `./install.sh` to turn on the nightly run.
  "Forget" deletes the matching facts, but a person's file keeps its name and heading (`people/priya.md`,
  `# Priya`), and the memory's git history keeps everything it ever held; delete the file and its history
  yourself if the name must go too.
- It reminds you. "Remind me to call mum at 5" (or every day, or every week) is said aloud at that time
  if you are at the laptop. If the screen is locked or the orb is not running, it goes to your Telegram
  instead, when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are in `.env` (see
  [GETTING_STARTED.md](GETTING_STARTED.md)); without Telegram it waits and is said when you are back.
- It is for errands, not coding or general computer tasks. Today that means research done on the web:
  finding and comparing flights, products, plans and prices, and answering questions about them.
- It is a Linux desktop app. The tray menu needs a desktop with a system tray (on GNOME, the
  AppIndicator extension); see [docs/sway.md](docs/sway.md) for Sway.

To set it up, see [GETTING_STARTED.md](GETTING_STARTED.md).

## How it works

```
mic -> Nemotron STT -> Smart Turn v3 -> front LLM -> Kokoro TTS -> speaker
                                          |  ^
                                 delegate(task)  result / questions
                                          v  |
                  Claude Code CLI (your login) in a sandboxed task folder
                          hooks -> local relay -> asks you by voice
```

Built on [Pipecat](https://docs.pipecat.ai).

## Folders

An errand works in its own private folder under `~/.voice-instinct/workspace`. To use one of your
folders ("the notes in ~/Documents/trip"), Nova asks first and says exactly what it would do: read
only, or read and change but never delete. Answer "yes" for just this errand (and its follow-ups), or
"always" to let every later errand use that folder at that level without asking; a grant covers the
folder's subfolders, and the most specific one wins. You can also say "you can always read my
Documents folder" up front, and it still asks you to confirm.

Deleting is asked for only when you say to delete something, is only ever for that one errand, and
each delete is asked again before it runs. Hidden folders (`~/.config`, `~/.local`, ...), this app's
own code and data, your whole home folder and anything outside it can never be granted.

What a grant does not cover yet: it limits what an errand writes, not what it reads. An errand can
still read files outside its grant (anything but the secrets listed below), without asking. A shell
command that reaches a folder without naming it (through a variable, a script, or `cd` and a relative
path) is not asked about, though the sandbox still stops it writing outside its workspace and the
granted folder. And deletes are caught only as shell commands (`rm`, `mv`, `find -delete`, ...): a
program that deletes files itself (`python -c ...`, a script) is not caught inside a folder it may
change.

Ask "what folders can you access?" to hear the standing grants, and "stop accessing Documents" to
remove one (no confirmation needed). They live in `~/.voice-instinct/folders.json`, read on every
errand, so you can also edit it by hand; an entry for a folder that can never be granted is ignored.
Errands can neither read nor change that file.

## How it keeps you in control

- Shell commands run inside Claude Code's sandbox, can only write inside the task's own folder (and a
  folder you let it change), and fail closed if the sandbox is unavailable.
- Your secrets (`~/.ssh`, cloud credentials, this app's keys and database) are off limits to the agent,
  and the front model's API key is removed from the agent's environment.
- The agent browses in its own headless browser that starts empty for every errand and is never
  signed in to your accounts. It gets none of your other Claude Code tools or connectors.
- When Claude Code asks for permission, your yes or no is decided by code from your own words, never
  by the model, and only after it actually asked you. Clicks and form entries in the errand browser
  are not asked one by one: Claude Code's auto-mode safety checks approve or refuse them, and the
  browser's tools that could run code or upload your files are turned off. A click, form fill, key
  press, dropdown choice or submitted text whose button, fields or page has one of these whole words is
  always asked first: pay, payment, buy, purchase, complete purchase, order, checkout, check out,
  subscribe, subscription, trial, upgrade, renew, confirm, confirm booking, book, booking, book now,
  complete booking, reserve, reservation, request ride, get tickets, donate, bid, place bid. "Add to
  cart", "reorder" and "bookmark" don't count. If that check fails, the step is refused.
- It is instructed never to buy, pay, book or cancel without your explicit go-ahead.
- The microphone stays open so it can hear the wake phrase. What it hears is transcribed on your
  machine and thrown away until you say the phrase; it is not stored or sent anywhere.
- Speech recognition and synthesis run locally, and sessions and questions are logged only on your
  machine in `~/.voice-instinct/`, readable only by you. The models you choose see the conversation
  and the task (Gemini and Claude are cloud services); use Ollama for a local front model.
- Say "bye" or "stop" to send it away; running errands carry on.
- Errands can read your memory but never change it, and never see the raw journal of your conversations.
  The nightly memory run has no network and no web tools. It learns from what you and Nova said, Nova's
  spoken summaries of errand results included, never the raw text of what a web page said. A day's journal
  is deleted once the nightly run has filed it and it is over 7 days old (one a failed run could not file is
  kept until a run does); `./install.sh --uninstall --purge` deletes all memory.

Using a subscription CLI this way depends on the vendor's terms; check them before relying on it.

## License

Copyright (C) 2026 Deepak Kumar Soni. Licensed under the [GNU AGPL v3.0](LICENSE). You may use, change and share it, but if you distribute it, or let
people use a modified version over a network, you must give them its full source under the same
license.

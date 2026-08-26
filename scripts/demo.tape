# VHS tape for the README demo GIF (https://github.com/charmbracelet/vhs).
#
# Records real `./foundry` CLI output only -- no staged Telegram
# conversation, no fabricated agent output. Requires a Foundry host with
# Docker, GPU/API secrets already configured (`./scripts/start.sh` run
# once), so `up` is the documented no-op-on-secrets restart path, not a
# fresh onboarding wizard.
#
# Generate:
#   vhs scripts/demo.tape
# Output lands at assets/demo.gif -- embed it in README.md once generated.

Output assets/demo.gif

Set Shell bash
Set FontSize 16
Set Width 1200
Set Height 650
Set Theme "Dracula"
Set Padding 20
Set TypingSpeed 60ms

Type "./foundry --help"
Enter
Sleep 2s

Type "./foundry up"
Enter
Sleep 8s

Type "./foundry status"
Enter
Sleep 3s

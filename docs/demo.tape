# The README demo GIF, recorded with VHS (https://github.com/charmbracelet/vhs) on SAMPLE data:
#   python scripts/demo_data.py ~/.anamnesis-demo && vhs docs/demo.tape && rm -rf ~/.anamnesis-demo

Output docs/demo.gif
Require anam

Set Shell "bash"
Set FontSize 15
Set Width 1240
Set Height 780
Set Padding 26
Set TypingSpeed 55ms
Set WindowBar Colorful
Set BorderRadius 10
Set Theme { "name": "anamnesis", "background": "#121110", "foreground": "#ece8e1", "cursor": "#e07a4f", "selection": "#3a332d", "black": "#121110", "red": "#e5534b", "green": "#5fbf8f", "yellow": "#e0b050", "blue": "#6fa8dc", "magenta": "#d97757", "cyan": "#8e9cf0", "white": "#ece8e1", "brightBlack": "#8a8378", "brightRed": "#e5534b", "brightGreen": "#5fbf8f", "brightYellow": "#e0b050", "brightBlue": "#6fa8dc", "brightMagenta": "#e07a4f", "brightCyan": "#8e9cf0", "brightWhite": "#ffffff" }

# Sample memory only, and no background import of the real history on this machine.
Hide
Type "export ANAMNESIS_DATA_DIR=$HOME/.anamnesis-demo/data CLAUDE_CONFIG_DIR=$HOME/.anamnesis-demo/claude ANAMNESIS_MEMORY_SYNC=false"
Enter
Type `PS1='\[\e[38;2;224;122;79m\]❯\[\e[0m\] '; clear`
Enter
Show

# 1. The archive
Sleep 600ms
Type "anam status"
Sleep 400ms
Enter
Sleep 5s

# 2. Search from the shell: just type what you're looking for
Hide
Type "clear"
Enter
Show
Type "anam why session cookies not jwt"
Sleep 400ms
Enter
Sleep 4.5s

# 3. The terminal UI: fuzzy filter, vim keys
Hide
Type "clear"
Enter
Show
Type "anam"
Enter
Sleep 2.5s
Type "/"
Sleep 600ms
Type@120ms "sess"
Sleep 2s
Escape
Sleep 700ms
Escape
Sleep 700ms
Type "j"
Sleep 350ms
Type "j"
Sleep 350ms
Type "k"
Sleep 350ms
Type "k"
Sleep 350ms
Type "l"
Sleep 3s
Ctrl+D
Sleep 900ms
Ctrl+U
Sleep 900ms
Type "h"
Sleep 600ms
Type "G"
Sleep 600ms
Type "g"
Sleep 800ms
Type "/"
Sleep 400ms
Type@100ms "rate limiting login"
Sleep 600ms
Enter
Sleep 3.5s
Ctrl+Q
Sleep 500ms

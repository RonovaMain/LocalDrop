#!/bin/sh
# LocalDrop @VERSION@ - self-extracting installer for Linux
#   ./LocalDrop-@VERSION@-x86_64.run            install (user: ~/.local, root: /opt)
#   ./LocalDrop-@VERSION@-x86_64.run --uninstall
APP_NAME="LocalDrop"
APP_VERSION="@VERSION@"
APP_EXE="@EXE@"
PAYLOAD_SHA256="@SHA256@"
MARKER=".localdrop-install"
SELF="$0"

usage() {
  cat <<USAGE
LocalDrop $APP_VERSION installer

Usage: $(basename "$SELF") [options]
  --prefix DIR     install into DIR (default: ~/.local/share/LocalDrop, as root /opt/LocalDrop)
  --no-desktop     do not create a menu entry and icon
  --uninstall      remove LocalDrop
  --extract DIR    only unpack the files into DIR
  -h, --help       this help
USAGE
}

MODE=install; PREFIX=""; DESKTOP=1; EXTRACT_TO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) MODE=uninstall ;;
    --prefix) shift; PREFIX="$1" ;;
    --prefix=*) PREFIX="${1#--prefix=}" ;;
    --no-desktop) DESKTOP=0 ;;
    --extract) shift; EXTRACT_TO="$1"; MODE=extract ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

if [ "$(id -u)" = "0" ]; then
  INSTALL_DIR="${PREFIX:-/opt/LocalDrop}"
  BIN_DIR="/usr/local/bin"
  APPS_DIR="/usr/share/applications"
  ICON_DIR="/usr/share/icons/hicolor/512x512/apps"
else
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
  INSTALL_DIR="${PREFIX:-$DATA/LocalDrop}"
  BIN_DIR="$HOME/.local/bin"
  APPS_DIR="$DATA/applications"
  ICON_DIR="$DATA/icons/hicolor/512x512/apps"
fi

do_uninstall() {
  if [ -L "$BIN_DIR/localdrop" ]; then
    case "$(readlink "$BIN_DIR/localdrop")" in "$INSTALL_DIR"/*) rm -f "$BIN_DIR/localdrop" ;; esac
  fi
  if [ -f "$INSTALL_DIR/$MARKER" ]; then
    rm -f "$APPS_DIR/localdrop.desktop" "$ICON_DIR/localdrop.png"
    rm -rf "$INSTALL_DIR"
    echo "$APP_NAME removed from $INSTALL_DIR"
  else
    echo "No $APP_NAME installation found in $INSTALL_DIR"
  fi
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1
  return 0
}

if [ "$MODE" = "uninstall" ]; then do_uninstall; exit 0; fi

START=$(awk '/^__LOCALDROP_PAYLOAD__$/ { print NR + 1; exit }' "$SELF")
[ -n "$START" ] || { echo "Installer is damaged: payload not found" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL=$(tail -n +"$START" "$SELF" | sha256sum | cut -d' ' -f1)
  if [ "$ACTUAL" != "$PAYLOAD_SHA256" ]; then
    echo "Checksum mismatch: the installer file is damaged or incomplete" >&2
    exit 1
  fi
fi

if [ "$MODE" = "extract" ]; then
  mkdir -p "$EXTRACT_TO" && tail -n +"$START" "$SELF" | tar -xz -C "$EXTRACT_TO" && echo "Unpacked to $EXTRACT_TO"
  exit $?
fi

if [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ] && [ ! -f "$INSTALL_DIR/$MARKER" ]; then
  echo "$INSTALL_DIR already exists and is not a $APP_NAME installation. Choose another --prefix." >&2
  exit 1
fi

TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
tail -n +"$START" "$SELF" | tar -xz -C "$TMP" || { echo "Unpacking failed" >&2; exit 1; }
[ -f "$TMP/$APP_EXE" ] || { echo "Installer is damaged: $APP_EXE is missing" >&2; exit 1; }

echo "Installing $APP_NAME $APP_VERSION to $INSTALL_DIR ..."
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
cp -a "$TMP"/. "$INSTALL_DIR"/
chmod +x "$INSTALL_DIR/$APP_EXE"
: > "$INSTALL_DIR/$MARKER"
ln -sf "$INSTALL_DIR/$APP_EXE" "$BIN_DIR/localdrop"

# uninstall.sh next to the app (same paths as this install)
cat > "$INSTALL_DIR/uninstall.sh" <<UNINSTALL
#!/bin/sh
INSTALL_DIR="$INSTALL_DIR"; BIN_DIR="$BIN_DIR"; APPS_DIR="$APPS_DIR"; ICON_DIR="$ICON_DIR"
[ -L "\$BIN_DIR/localdrop" ] && rm -f "\$BIN_DIR/localdrop"
rm -f "\$APPS_DIR/localdrop.desktop" "\$ICON_DIR/localdrop.png"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "\$APPS_DIR" >/dev/null 2>&1
cd / && rm -rf "\$INSTALL_DIR"
echo "$APP_NAME removed"
UNINSTALL
chmod +x "$INSTALL_DIR/uninstall.sh"

if [ "$DESKTOP" = "1" ]; then
  mkdir -p "$APPS_DIR" "$ICON_DIR"
  [ -f "$TMP/localdrop-icon.png" ] && cp "$TMP/localdrop-icon.png" "$ICON_DIR/localdrop.png"
  cat > "$APPS_DIR/localdrop.desktop" <<DESKTOP_ENTRY
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Instant hosting of web projects on your local network
Exec="$INSTALL_DIR/$APP_EXE"
Icon=localdrop
Terminal=false
Categories=Development;Network;
DESKTOP_ENTRY
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1
  command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$(dirname "$(dirname "$(dirname "$ICON_DIR")")")" >/dev/null 2>&1
fi

echo "Done. Start it from the application menu or run: localdrop"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Note: $BIN_DIR is not in PATH." ;; esac
command -v zenity >/dev/null 2>&1 || command -v kdialog >/dev/null 2>&1 || \
  echo "Note: install 'zenity' (e.g. sudo apt install zenity) to get the file chooser dialog."
exit 0
__LOCALDROP_PAYLOAD__

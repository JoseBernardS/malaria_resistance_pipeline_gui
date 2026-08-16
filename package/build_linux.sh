#!/bin/bash
# Build a self-contained Linux distribution of the Pf Drug Resistance app.
#
# Produces a relocatable directory (build/PfDrugResistance/) containing a
# conda-pack'd environment (all bio tools + GUI deps), the app code, the
# Clair3 model and reference data, plus a launcher. If appimagetool is
# available, it is also wrapped into a single-file AppImage.
#
# As with macOS, the result is large because it ships native bio tools, the
# Clair3 model and the reference genome.
set -euo pipefail

ENV_NAME="${ENV_NAME:-nanopore_all}"
APP_NAME="PfDrugResistance"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_ROOT/build"
APPDIR="$BUILD_DIR/$APP_NAME.AppDir"

echo "==> Cleaning previous build"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/env" "$APPDIR/usr/app"

eval "$(conda shell.bash hook)"
if ! conda env list | grep -qE "^\s*${ENV_NAME}\s"; then
    echo "ERROR: conda env '$ENV_NAME' not found."
    echo "Run envs/install_pipeline_dependencies.sh first."
    exit 1
fi

echo "==> conda-pack '$ENV_NAME'"
PACK_TGZ="$BUILD_DIR/env.tar.gz"
conda pack -n "$ENV_NAME" -o "$PACK_TGZ" --force

echo "==> Unpacking env into AppDir"
tar -xzf "$PACK_TGZ" -C "$APPDIR/usr/env"
"$APPDIR/usr/env/bin/conda-unpack"

echo "==> Copying app code, refs and Clair3 model"
for d in gui src bin config data; do
    rsync -a --exclude '__pycache__' "$PROJECT_ROOT/$d" "$APPDIR/usr/app/"
done

echo "==> Writing AppRun launcher"
cat > "$APPDIR/AppRun" <<'LAUNCH'
#!/bin/bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV="$HERE/usr/env"
APP="$HERE/usr/app"
export PATH="$ENV/bin:$PATH"
export PYTHONPATH="$APP:$PYTHONPATH"
export CONDA_PREFIX="$ENV"
cd "$APP"
exec "$ENV/bin/python" -m gui.app "$@"
LAUNCH
chmod +x "$APPDIR/AppRun"

# Minimal .desktop + icon for AppImage tooling.
cat > "$APPDIR/$APP_NAME.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Pf Drug Resistance
Exec=AppRun
Icon=$APP_NAME
Categories=Science;
DESK
# placeholder icon (empty) so appimagetool is satisfied if no icon provided
: > "$APPDIR/$APP_NAME.png"

echo "==> Built $APPDIR"
echo "    Size: $(du -sh "$APPDIR" | cut -f1)"

if command -v appimagetool >/dev/null 2>&1; then
    echo "==> Creating AppImage"
    appimagetool "$APPDIR" "$BUILD_DIR/$APP_NAME-x86_64.AppImage"
    echo "    Wrote $BUILD_DIR/$APP_NAME-x86_64.AppImage"
else
    echo "==> appimagetool not found; run the app via $APPDIR/AppRun"
fi

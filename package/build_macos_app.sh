#!/bin/bash
# Build a self-contained, double-clickable PfDrugResistance.app for macOS.
#
# The .app bundles a relocatable conda environment (built with conda-pack)
# holding every bioinformatics tool (Clair3, minimap2, samtools, bcftools,
# bedtools) plus the GUI's Python deps (pyqt, psutil, openpyxl, matplotlib,
# reportlab), the Clair3 model and the reference data. A small launcher sets
# PATH/PYTHONPATH to the bundled env and runs `python -m gui.app`, so the
# bash pipeline finds all tools inside the bundle. No conda needed on the
# target machine.
#
# NOTE: the resulting bundle is large (multiple GB) because it ships native
# bio tools, the Clair3 PyTorch model and the P. falciparum reference.
set -euo pipefail

ENV_NAME="${ENV_NAME:-nanopore_all}"
APP_NAME="PfDrugResistance"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_ROOT/build"
APP_DIR="$BUILD_DIR/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
RESOURCES="$CONTENTS/Resources"
MACOS="$CONTENTS/MacOS"

echo "==> Cleaning previous build"
rm -rf "$APP_DIR"
mkdir -p "$RESOURCES/env" "$RESOURCES/app" "$MACOS"

# 1. Ensure the env exists with all deps.
eval "$(conda shell.bash hook)"
if ! conda env list | grep -qE "^\s*${ENV_NAME}\s"; then
    echo "ERROR: conda env '$ENV_NAME' not found."
    echo "Run envs/install_pipeline_dependencies.sh first."
    exit 1
fi

# 2. Pack the env into a relocatable tarball.
echo "==> conda-pack '$ENV_NAME'"
PACK_TGZ="$BUILD_DIR/env.tar.gz"
conda pack -n "$ENV_NAME" -o "$PACK_TGZ" --force

# 3. Unpack into Resources/env and fix prefixes.
echo "==> Unpacking env into bundle"
tar -xzf "$PACK_TGZ" -C "$RESOURCES/env"
"$RESOURCES/env/bin/conda-unpack"

# 4. Copy the application code + data (read-only).
echo "==> Copying app code, refs and Clair3 model"
for d in gui src bin config data; do
    rsync -a --exclude '__pycache__' "$PROJECT_ROOT/$d" "$RESOURCES/app/"
done

# 4b. App icon (mosquito+DNA emblem) shown in Finder/Dock.
echo "==> Installing app icon"
cp "$PROJECT_ROOT/gui/assets/AppIcon.icns" "$RESOURCES/AppIcon.icns"

# 5. Launcher.
echo "==> Writing launcher"
cat > "$MACOS/$APP_NAME" <<'LAUNCH'
#!/bin/bash
# Finder/LaunchServices runs this directly (no Terminal window). We still send
# all output to a log file so nothing ever writes to a controlling tty.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$(cd "$HERE/../Resources" && pwd)"
ENV="$RES/env"
APP="$RES/app"
export PATH="$ENV/bin:$PATH"
export PYTHONPATH="$APP:$PYTHONPATH"
export CONDA_PREFIX="$ENV"
LOG_DIR="$HOME/Library/Logs/PfDrugResistance"
mkdir -p "$LOG_DIR"
cd "$APP"
exec "$ENV/bin/python" -m gui.app "$@" >>"$LOG_DIR/app.log" 2>&1
LAUNCH
chmod +x "$MACOS/$APP_NAME"

# 6. Info.plist.
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>Pf Drug Resistance</string>
    <key>CFBundleIdentifier</key><string>org.pfdrugresistance.app</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>$APP_NAME</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSBackgroundOnly</key><false/>
</dict>
</plist>
PLIST

echo "==> Built $APP_DIR"
echo "    Size: $(du -sh "$APP_DIR" | cut -f1)"

# 7. Optionally wrap as a .dmg (set MAKE_DMG=1).
if [[ "${MAKE_DMG:-0}" == "1" ]]; then
    echo "==> Creating .dmg"
    hdiutil create -volname "$APP_NAME" -srcfolder "$APP_DIR" \
        -ov -format UDZO "$BUILD_DIR/$APP_NAME.dmg"
    echo "    Wrote $BUILD_DIR/$APP_NAME.dmg"
fi

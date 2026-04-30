#!/bin/bash
set -euo pipefail

echo "[post_install] Starting powergrid post-install configuration..."

# --- Link host home folder files (ignore if missing) ---
for f in .netrc .zsh_history .bash_history; do
	if [ -e "/host-home-folder/$f" ] && [ ! -e "/home/codespace/$f" ]; then
		ln -s "/host-home-folder/$f" "/home/codespace/$f" || true
	fi
done

# --- ZSH plugins (only if oh-my-zsh is present & plugins not already cloned) ---
if [ -d "$HOME/.oh-my-zsh" ]; then
	PLUGIN_DIR="$HOME/.oh-my-zsh/custom/plugins"
	mkdir -p "$PLUGIN_DIR"
	if [ ! -d "$PLUGIN_DIR/zsh-syntax-highlighting" ]; then
		git clone --depth 1 https://github.com/zsh-users/zsh-syntax-highlighting.git "$PLUGIN_DIR/zsh-syntax-highlighting" || true
	fi
	if [ ! -d "$PLUGIN_DIR/zsh-autosuggestions" ]; then
		git clone --depth 1 https://github.com/zsh-users/zsh-autosuggestions "$PLUGIN_DIR/zsh-autosuggestions" || true
	fi
	# Add plugins if not already present
	if grep -q "plugins=(git)" "$HOME/.zshrc"; then
		sed -i 's/plugins=(git)/plugins=(git zsh-syntax-highlighting zsh-autosuggestions)/' "$HOME/.zshrc"
	fi
else
	echo "[post_install] Skipping zsh plugin setup (oh-my-zsh not found)."
fi

# --- Ensure conda is available ---
CONDA_SH="/opt/mamba-forge/etc/profile.d/conda.sh"
if [ -f "$CONDA_SH" ]; then
	# shellcheck disable=SC1090
	. "$CONDA_SH"
else
	echo "[post_install] ERROR: conda initialization script not found at $CONDA_SH" >&2
	echo "[post_install] Skipping conda-dependent steps."
	exit 0
fi

# Initialize conda for zsh (idempotent) & ensure default env activation in interactive shells
if ! grep -q "conda activate agoragrids" "$HOME/.zshrc"; then
	conda init zsh || true
	echo 'conda activate agoragrids' >> "$HOME/.zshrc"
fi


cd /agora/src/domains/powergrid
git submodule update --init --recursive || true

# Remove pypsa-usa's separate virtual environment if it exists
# We want to use the parent powergrid environment for everything
if [ -d "server/external/pypsa-usa/.venv" ]; then
	echo "[post_install] Removing pypsa-usa's separate virtual environment..."
	rm -rf server/external/pypsa-usa/.venv
fi

# Check if CUDA is available and set environment variables for HiGHS build
if command -v nvcc &> /dev/null; then
	echo "[post_install] CUDA detected at $(which nvcc), enabling GPU support for HiGHS..."
	export HIGHS_BUILD_GPU=ON
	export HIGHS_CUDA_FIND=ON
	nvcc --version
else
	echo "[post_install] CUDA not detected, building HiGHS without GPU support..."
	export HIGHS_BUILD_GPU=OFF
	export HIGHS_CUDA_FIND=OFF
fi

# uv sync will build HiGHS from source using scikit-build-core
# The environment variables above control GPU compilation
echo "[post_install] Building and installing packages (including HiGHS with GPU=$HIGHS_BUILD_GPU)..."
uv sync --locked

uv tool install pyright
uv tool install pre-commit
pre-commit install

echo "[post_install] Note: Always run 'uv run' from /agora/src/domains/powergrid to use the shared environment with GPU-enabled HiGHS"

echo "powergrid dev container setup complete!"
echo "Run python scripts with 'uv run python <script>' to ensure the correct environment is used."

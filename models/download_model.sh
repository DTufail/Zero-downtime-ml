#!/bin/bash
# Downloads the SmolLM2-1.7B GGUF (INT4 quantized) from HuggingFace
set -e

MODEL_DIR="${1:-./models}"
MODEL_FILE="$MODEL_DIR/smollm2.gguf"
REPO="bartowski/SmolLM2-1.7B-Instruct-GGUF"
FILENAME="SmolLM2-1.7B-Instruct-Q4_K_M.gguf"

mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_FILE" ]; then
    echo "Model already exists at $MODEL_FILE, skipping download."
    echo "Delete it first if you want to re-download."
    exit 0
fi

echo "Downloading SmolLM2-1.7B-Instruct GGUF (Q4_K_M)..."
echo "Source: https://huggingface.co/$REPO"
echo "Target: $MODEL_FILE"

# Try huggingface-cli first
if command -v huggingface-cli &> /dev/null; then
    echo "Using huggingface-cli..."
    huggingface-cli download "$REPO" "$FILENAME" --local-dir "$MODEL_DIR"
    mv "$MODEL_DIR/$FILENAME" "$MODEL_FILE"
else
    echo "huggingface-cli not found, using curl..."
    curl -L -o "$MODEL_FILE" \
        "https://huggingface.co/$REPO/resolve/main/$FILENAME"
fi

# Verify file size (expect ~900MB - 1.2GB)
if [ "$(uname)" = "Darwin" ]; then
    FILE_SIZE=$(stat -f%z "$MODEL_FILE")
else
    FILE_SIZE=$(stat --printf="%s" "$MODEL_FILE")
fi

SIZE_MB=$(echo "scale=2; $FILE_SIZE/1024/1024" | bc)
echo "Downloaded: ${SIZE_MB}MB"

if [ "$FILE_SIZE" -lt 500000000 ]; then
    echo "WARNING: File seems too small (< 500MB). Download may have failed."
    exit 1
fi

echo "Done. Model saved to $MODEL_FILE"

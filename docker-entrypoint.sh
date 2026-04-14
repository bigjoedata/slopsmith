#!/bin/bash
set -e

# Default to auto-updating plugins if present
AUTO_UPDATE_PLUGINS=${AUTO_UPDATE_PLUGINS:-true}

# If INSTALL_PLUGINS is provided (comma-separated list of github clone URLs)
if [ -n "$INSTALL_PLUGINS" ]; then
    echo "[Entrypoint] Managing plugins from INSTALL_PLUGINS..."
    # Format: url[:dir_name]
    IFS=',' read -ra ADDR <<< "$INSTALL_PLUGINS"
    for i in "${ADDR[@]}"; do
        # Trim whitespace
        i=$(echo "$i" | xargs)
        
        # Skip empty strings
        if [ -z "$i" ]; then continue; fi
        
        repo="${i%%:*}"
        dirname="${i##*:}"
        
        # If no dir name specified, extract from repo URL (strip .git)
        if [ "$repo" = "$dirname" ]; then
            dirname=$(basename -s .git "$repo")
        fi
        
        if [ ! -d "/app/plugins/$dirname/.git" ]; then
            echo "[Entrypoint] Cloning $repo into /app/plugins/$dirname..."
            git clone "$repo" "/app/plugins/$dirname" || echo "[Entrypoint] Failed to clone $repo"
        else
            if [ "$AUTO_UPDATE_PLUGINS" = "true" ]; then
                echo "[Entrypoint] Updating /app/plugins/$dirname..."
                (cd "/app/plugins/$dirname" && git pull) || echo "[Entrypoint] Failed to pull $repo"
            fi
        fi
    done
fi

if [ -n "$DISABLE_PLUGINS" ]; then
    echo "[Entrypoint] Processing DISABLE_PLUGINS..."
    IFS=',' read -ra ADDR <<< "$DISABLE_PLUGINS"
    for i in "${ADDR[@]}"; do
        i=$(echo "$i" | xargs)
        if [ -z "$i" ]; then continue; fi
        
        if [ -d "/app/plugins/$i" ]; then
            echo "[Entrypoint] Disabling/Removing plugin '$i'..."
            rm -rf "/app/plugins/$i"
        fi
    done
fi

echo "[Entrypoint] Starting Slopsmith server..."
exec uvicorn server:app --host 0.0.0.0 --port 8000

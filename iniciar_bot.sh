#!/usr/bin/env bash
# iniciar o bot de discord (um ao infinito)
# - abre um terminal sozinho se for executado por duplo clique
# - cria o venv e instala o discord.py se ainda nao existirem
# - le o token da variavel DISCORD_TOKEN ou do arquivo token.txt (nesta pasta)

SCRIPT="$(readlink -f "$0")"
cd "$(dirname "$SCRIPT")" || exit 1

# se nao estiver rodando dentro de um terminal, abre um e roda este script nele
if [ ! -t 1 ]; then
    for term in gnome-terminal mate-terminal xfce4-terminal x-terminal-emulator konsole xterm; do
        if command -v "$term" >/dev/null 2>&1; then
            case "$term" in
                gnome-terminal|mate-terminal) exec "$term" -- bash "$SCRIPT" ;;
                xfce4-terminal)               exec "$term" -e "bash '$SCRIPT'" ;;
                *)                            exec "$term" -e bash "$SCRIPT" ;;
            esac
        fi
    done
    echo "nenhum terminal encontrado. rode este script de dentro de um terminal."
    exit 1
fi

if [ ! -d venv ]; then
    echo "criando ambiente virtual..."
    if ! python3 -m venv venv; then
        echo "falha ao criar o venv. tente: sudo apt install python3-venv"
        read -rp "pressione enter para fechar."
        exit 1
    fi
fi

# shellcheck disable=SC1091
source venv/bin/activate

if ! python -c "import discord" 2>/dev/null; then
    echo "instalando discord.py..."
    pip install -U discord.py || { read -rp "falha na instalacao. pressione enter para fechar."; exit 1; }
fi

echo "iniciando o bot (ctrl+c para parar)..."
python bot_um_ao_infinito.py

echo
read -rp "o bot parou. pressione enter para fechar."

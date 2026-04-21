---
title: Kick Clip Downloader
emoji: "🎬"
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Kick Clip Downloader

Site para baixar clips publicos da Kick e entregar o resultado final em `.mp4` com video em H.264.

## Requisitos

- Python 3.12
- `ffmpeg` instalado no sistema

## Como rodar

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Depois abra `http://127.0.0.1:5000`.

## O que o site faz

- aceita links publicos da Kick no formato `https://kick.com/<canal>/clips/clip_<id>`
- baixa o clip usando `yt-dlp`
- converte o arquivo final para `.mp4` em H.264 com `ffmpeg`

## Observacoes

- o processamento depende do clip ainda estar publico na Kick
- como a Kick muda a plataforma com frequencia, manter o `yt-dlp` atualizado ajuda bastante
- no teste feito em 17 de abril de 2026, Python 3.12 funcionou melhor que Python 3.14 para esse fluxo da Kick

## Deploy no Hugging Face Spaces

O projeto inclui um `Dockerfile` pronto para Hugging Face Spaces com `ffmpeg` instalado.

- use um Space do tipo `Docker`
- a aplicacao usa a variavel `PORT` da plataforma automaticamente
- o servidor de producao e o `gunicorn`

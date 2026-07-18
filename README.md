# Shadow President

[![GLWTSPL](https://img.shields.io/badge/GLWTS-Public_License-red.svg)](https://github.com/me-shaon/GLWTPL)

BepInEx plugin to allow LLMs to play Suzerain. Only supports Sordland campaign.

Requires BepInEx 6.0 Il2Cpp (not BepInEx 5). Drop the compiled plugin into `BepInEx/plugins`, run `Server/server.py`, start up an OpenAI-compatible endpoint (e.g. LM Studio), and open your browser to `localhost:1954`. Configure the model and prompts in `Server/config.json`.

Technically supports live online viewing through a tunnel, so you can broadcast the run in real-time.

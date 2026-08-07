name: Richiami Italia Updater

on:
  workflow_dispatch:

  schedule:
    - cron: "0 6 * * *"

jobs:
  update:
    runs-on: ubuntu-latest

    steps:
      - name: Scarica repository
        uses: actions/checkout@v4

      - name: Configura Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Installa dipendenze
        run: pip install -r requirements.txt

      - name: Test collegamento Ministero
        run: python ministry_fetcher.py

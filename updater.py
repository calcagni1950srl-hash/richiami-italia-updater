from datetime import datetime, timezone


def main():
    now = datetime.now(timezone.utc)

    print("====================================")
    print("RICHIAMI ITALIA - UPDATER")
    print("====================================")
    print("Updater avviato correttamente.")
    print(f"Data e ora UTC: {now.isoformat()}")
    print("Test completato con successo.")


if __name__ == "__main__":
    main()

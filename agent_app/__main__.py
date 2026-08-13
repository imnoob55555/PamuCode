from .cli import main


if __name__ == "__main__":
    status = main()
    if status is not None:
        raise SystemExit(status)

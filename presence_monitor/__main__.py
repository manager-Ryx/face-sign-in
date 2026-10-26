import os

if __name__ == "__main__":
    # numpy's BLAS would otherwise reserve a buffer per CPU core (hundreds of MB
    # of committed memory) for the tray's tiny arrays.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    # Absolute import so this also works as a frozen (PyInstaller) entry point.
    from presence_monitor.app import main
    main()

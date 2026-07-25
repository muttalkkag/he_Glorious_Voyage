from pathlib import Path
import sys
import traceback

base = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
log = base / "startup_error.log"

try:
    from app import main
    main()
except Exception:
    error_text = traceback.format_exc()
    try:
        log.write_text(error_text, encoding="utf-8")
    except Exception:
        pass

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "의지의 바다 무역 계산기 실행 오류",
            "프로그램 실행 중 오류가 발생했습니다.\n\n"
            f"오류 기록:\n{log}\n\n"
            f"{error_text[-1200:]}",
        )
        root.destroy()
    except Exception:
        pass

    raise SystemExit(1)

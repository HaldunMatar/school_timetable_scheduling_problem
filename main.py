#!/usr/bin/env python3
"""
نقطة تشغيل البرنامج.

تشغيل:  python3 main.py
"""

import sys


def main():
    try:
        import gui
    except ImportError as exc:
        print("خطأ: تعذّر تحميل واجهة البرنامج (tkinter).")
        print(f"التفاصيل: {exc}")
        print("راجع ملف README.md لتعليمات تثبيت tkinter.")
        sys.exit(1)

    gui.main()


if __name__ == "__main__":
    main()

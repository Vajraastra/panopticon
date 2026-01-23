
try:
    import ultralytics
    print(f"Ultralytics version: {ultralytics.__version__}")
    print("Import successful.")
except ImportError:
    print("Ultralytics NOT installed.")
except Exception as e:
    print(f"Error importing ultralytics: {e}")

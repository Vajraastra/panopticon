import os
import sys
from collections import Counter
import tkinter as tk
from tkinter import filedialog

def run_scanner():
    # 1. Setup UI to pick folder
    root = tk.Tk()
    root.withdraw() # Hide main window
    
    print("--- Panopticon: Image Format Statistics ---")
    folder_path = filedialog.askdirectory(title="Selecciona la carpeta para analizar")
    
    if not folder_path:
        print("Operación cancelada.")
        return

    print(f"Escaneando (recursivamente): {folder_path}")
    print("Esto puede tardar unos segundos dependiendo del volumen de archivos...")
    
    stats = Counter()
    total_files = 0
    img_extensions = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.gif', '.ico', '.svg')
    
    try:
        for root_dir, _, files in os.walk(folder_path):
            for f in files:
                total_files += 1
                ext = os.path.splitext(f)[1].lower()
                if ext in img_extensions:
                    stats[ext] += 1
                
                # Feedback visual cada 1000 archivos
                if total_files % 1000 == 0:
                    print(f"Archivos analizados: {total_files}...", end='\r')
        
    except KeyboardInterrupt:
        print("\nEscaneo interrumpido por el usuario.")
    except Exception as e:
        print(f"\nError durante el escaneo: {e}")

    # 2. Results
    print("\n\n" + "="*40)
    print("     RESULTADOS ESTADÍSTICOS")
    print("="*40)
    
    total_imgs = sum(stats.values())
    if total_imgs == 0:
        print(f"No se encontraron imágenes en los formatos soportados.")
        print(f"Archivos totales revisados: {total_files}")
    else:
        for ext, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            percent = (count / total_imgs) * 100
            print(f" {ext.upper():<7} | {count:>8} archivos | {percent:>6.2f}%")
        
        print("-" * 40)
        print(f" TOTAL IMÁGENES: {total_imgs}")
        print(f" OTROS ARCHIVOS: {total_files - total_imgs}")
        print(f" TOTAL ESCANEADO: {total_files}")
    
    print("="*40)
    input("\nPresiona ENTER para salir...")

if __name__ == "__main__":
    run_scanner()

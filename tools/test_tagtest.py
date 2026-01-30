"""
Test completo de transfer y verification usando tagtest/
"""
import sys
sys.path.insert(0, '.')
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import shutil
from pathlib import Path
from core.paths import CachePaths
from core.metadata import MetadataExtractor, MetadataStamper, MetadataVerifier

test_dir = Path('tagtest')
output_dir = CachePaths.get_temp_folder() / 'transfer_test'
output_dir.mkdir(parents=True, exist_ok=True)

print('=' * 60)
print('  FULL TRANSFER & VERIFICATION TEST')
print('=' * 60)
print(f'Source: {test_dir}')
print(f'Output: {output_dir}')

results = {'ok': 0, 'warning': 0, 'fail': 0}

for img in sorted(test_dir.glob('*')):
    if img.suffix.lower() not in ['.png', '.jpg', '.jpeg', '.webp']:
        continue
    
    print(f'\n{img.name}')
    print('-' * 40)
    
    # 1. Extract original
    orig_bundle = MetadataExtractor.extract(img)
    status = "Has data" if orig_bundle.is_valid() else "Empty"
    print(f'  Original: {status}')
    
    # 2. Copy file
    copy_path = output_dir / img.name
    shutil.copy2(img, copy_path)
    
    # 3. Transfer metadata
    transfer = MetadataStamper.transfer(img, copy_path)
    preserved = "preserved" if transfer.metadata_preserved else "none"
    print(f'  Transfer: {"OK" if transfer.success else "FAIL"} ({preserved})')
    
    # 4. Verify
    verify = MetadataVerifier.verify_transfer(img, copy_path)
    print(f'  Verify: {verify.status} ({verify.integrity_score}%)')
    
    if verify.missing:
        print(f'    Missing: {verify.missing}')
    
    results[verify.status.lower()] = results.get(verify.status.lower(), 0) + 1

print('\n' + '=' * 60)
print('  SUMMARY')
print('=' * 60)
print(f'  OK:      {results.get("ok", 0)}')
print(f'  WARNING: {results.get("warning", 0)}')
print(f'  FAIL:    {results.get("fail", 0)}')
print(f'\nOutput files in: {output_dir}')

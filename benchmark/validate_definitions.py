#!/usr/bin/env python3
"""
Validate benchmark definition files.

Checks that:
- All definition files are valid JSON
- IDs are unique
- Referenced runner scripts exist
- Referenced fixtures exist
- Referenced normalization scripts exist
- Referenced normalization functions exist (where specified)

Usage:
    python benchmark/validate_definitions.py
"""

import json
import os
import sys


def validate_definitions(definitions_dir):
    """Validate all definition files in the given directory."""
    errors = []
    warnings = []
    definitions = []
    
    # Find all JSON files
    if not os.path.isdir(definitions_dir):
        print(f"ERROR: Definitions directory not found: {definitions_dir}")
        return False
    
    for filename in sorted(os.listdir(definitions_dir)):
        if not filename.endswith('.json'):
            continue
        
        filepath = os.path.join(definitions_dir, filename)
        try:
            with open(filepath, encoding='utf-8') as fh:
                data = json.load(fh)
        except json.JSONDecodeError as e:
            errors.append(f"Invalid JSON in {filename}: {e}")
            continue
        except Exception as e:
            errors.append(f"Error reading {filename}: {e}")
            continue
        
        # Check required fields
        if 'id' not in data:
            errors.append(f"{filename}: missing 'id' field")
            continue
        
        if 'name' not in data:
            warnings.append(f"{filename}: missing 'name' field")
        
        if 'runner' not in data:
            warnings.append(f"{filename}: missing 'runner' field")
        
        # Check runner script exists
        runner = data.get('runner')
        if runner:
            runner_path = os.path.join(os.path.dirname(definitions_dir), '..', runner)
            if not os.path.exists(runner_path):
                errors.append(f"{filename}: runner script not found: {runner}")
        
        # Check fixtures exist
        fixtures = data.get('fixtures', [])
        for fixture in fixtures:
            fixture_path = os.path.join(os.path.dirname(definitions_dir), '..', fixture)
            if not os.path.exists(fixture_path):
                errors.append(f"{filename}: fixture not found: {fixture}")
        
        # Check normalization script exists
        norm = data.get('normalization', {})
        if norm and 'script' in norm:
            norm_path = os.path.join(os.path.dirname(definitions_dir), '..', norm['script'])
            if not os.path.exists(norm_path):
                errors.append(f"{filename}: normalization script not found: {norm['script']}")
            
            # Check function exists in script (if specified)
            if 'function' in norm:
                try:
                    with open(norm_path, encoding='utf-8') as fh:
                        content = fh.read()
                    if f"def {norm['function']}(" not in content:
                        errors.append(f"{filename}: normalization function not found: {norm['function']}")
                except Exception as e:
                    warnings.append(f"{filename}: could not check normalization function: {e}")
        
        definitions.append(data)
    
    # Check for duplicate IDs
    ids = [d.get('id') for d in definitions]
    seen = set()
    for id_ in ids:
        if id_ in seen:
            errors.append(f"Duplicate ID: {id_}")
        seen.add(id_)
    
    # Print results
    if errors:
        print("ERRORS:")
        for error in errors:
            print(f"  [ERROR] {error}")
    
    if warnings:
        print("\nWARNINGS:")
        for warning in warnings:
            print(f"  [WARN] {warning}")
    
    if not errors and not warnings:
        print("[OK] All definitions valid")
    
    print(f"\nValidated {len(definitions)} definitions")
    return len(errors) == 0


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    definitions_dir = os.path.join(script_dir, 'definitions')
    
    success = validate_definitions(definitions_dir)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()

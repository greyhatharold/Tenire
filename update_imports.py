#!/usr/bin/env python3
"""
Script to update imports from signal_servicer.py to use the new imports from tenire.servicers.
"""

import os
import re

def update_file(filepath):
    """Update imports in a single file."""
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Replace direct imports from signal_servicer
    content = re.sub(
        r'from tenire\.servicers\.signal_servicer import (.*)',
        r'from tenire.servicers import \1',
        content
    )
    
    with open(filepath, 'w') as f:
        f.write(content)

def main():
    """Main function to update all imports."""
    base_dir = 'src/tenire'
    for root, _, files in os.walk(base_dir):
        for file in files:
            if file.endswith('.py'):
                filepath = os.path.join(root, file)
                if 'signal_servicer.py' not in filepath and 'signal.py' not in filepath:
                    update_file(filepath)

if __name__ == '__main__':
    main() 
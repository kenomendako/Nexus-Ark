#!/usr/bin/env python3
"""
Safely add allow_custom_value=True to all gr.Dropdown definitions.
Handles single-line definitions with multiple components (semicolon separated).
"""
import re
import sys

def add_allow_custom_value(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    changes = 0
    
    # Find all gr.Dropdown(...) patterns and add allow_custom_value=True if not present
    def replace_dropdown(match):
        nonlocal changes
        full_match = match.group(0)
        
        # Skip if already has allow_custom_value
        if 'allow_custom_value' in full_match:
            return full_match
        
        # Find the last ) and insert before it
        # Need to handle nested parentheses
        depth = 0
        last_paren_idx = -1
        for i, c in enumerate(full_match):
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    last_paren_idx = i
                    break
        
        if last_paren_idx == -1:
            return full_match
        
        # Check if there's content before the closing paren
        before = full_match[:last_paren_idx].rstrip()
        after = full_match[last_paren_idx:]
        
        if before.endswith(','):
            # Already has trailing comma
            new_match = before + ' allow_custom_value=True' + after
        else:
            # Need to add comma
            new_match = before + ', allow_custom_value=True' + after
        
        changes += 1
        return new_match
    
    # Match gr.Dropdown followed by balanced parentheses
    # This regex matches gr.Dropdown( followed by content with balanced parens
    def find_and_replace_dropdowns(text):
        result = []
        i = 0
        while i < len(text):
            # Look for gr.Dropdown(
            idx = text.find('gr.Dropdown(', i)
            if idx == -1:
                result.append(text[i:])
                break
            
            # Add everything before this match
            result.append(text[i:idx])
            
            # Find the matching closing parenthesis
            start = idx
            paren_start = idx + len('gr.Dropdown')
            depth = 0
            j = paren_start
            while j < len(text):
                if text[j] == '(':
                    depth += 1
                elif text[j] == ')':
                    depth -= 1
                    if depth == 0:
                        # Found the closing paren
                        dropdown_def = text[start:j+1]
                        replaced = replace_dropdown(type('Match', (), {'group': lambda self, x: dropdown_def})())
                        result.append(replaced)
                        i = j + 1
                        break
                j += 1
            else:
                # No closing paren found, just add the rest
                result.append(text[i:])
                break
        
        return ''.join(result)
    
    new_content = find_and_replace_dropdowns(content)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print(f"Modified {changes} Dropdown definitions in {filepath}")
    return changes

if __name__ == '__main__':
    filepath = sys.argv[1] if len(sys.argv) > 1 else 'nexus_ark.py'
    add_allow_custom_value(filepath)

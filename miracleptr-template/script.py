import subprocess
import os
import csv
import concurrent.futures
from collections import defaultdict

import tree_sitter_cpp
from tree_sitter import Language, Parser, Query, QueryCursor

# ==========================================
# 1. Global Tree-sitter Initialization
# (Must be global so each worker process initializes its own copy natively)
# ==========================================
CPP_LANGUAGE = Language(tree_sitter_cpp.language())
parser = Parser(CPP_LANGUAGE)

QUERY_STRING = """
(field_declaration
  type: [
    (template_type) @tmpl
    (qualified_identifier (template_type) @tmpl)
  ]
)
"""
query = Query(CPP_LANGUAGE, QUERY_STRING)

# ==========================================
# 2. Helper Functions
# ==========================================

def get_git_files():
    """Run git ls-files and return C/C++ file paths, filtering third_party."""
    print("Running git ls-files to discover source files...")
    try:
        result = subprocess.run(
            ['git', 'ls-files'], 
            capture_output=True, 
            text=True, 
            check=True
        )
        files = result.stdout.splitlines()
        
        cpp_files = []
        for f in files:
            # Only process standard C/C++ extensions
            if not f.endswith(('.h', '.hpp', '.cc', '.cpp')):
                continue
                
            # Ignore third_party/ UNLESS it is third_party/blink/
            if f.startswith('third_party/') and not f.startswith('third_party/blink/'):
                continue
                
            cpp_files.append(f)
            
        return cpp_files
    except subprocess.CalledProcessError as e:
        print(f"Error running git ls-files: {e}")
        return []

def get_container_name(tmpl_node):
    name_node = tmpl_node.child_by_field_name('name')
    if not name_node:
        return None
        
    if name_node.type == 'scoped_type_identifier':
        final_name_node = name_node.child_by_field_name('name')
        if final_name_node:
            text = final_name_node.text.decode('utf8')
        else:
            text = name_node.text.decode('utf8').split('::')[-1]
    else:
        text = name_node.text.decode('utf8')
    
    return text.split('<')[0].strip()

def is_raw_ptr_type(node):
    text = node.text.decode('utf8').replace(' ', '').replace('\n', '')
    if text.startswith('raw_ptr<') or text.startswith('base::raw_ptr<'):
        return True
    if node.type == 'template_type':
        name = get_container_name(node)
        if name in ('raw_ptr', 'base::raw_ptr'):
            return True
    for child in node.children:
        if is_raw_ptr_type(child):
            return True
    return False

def contains_function_declarator(node):
    if node.type in ('function_declarator', 'abstract_function_declarator'):
        return True
    for child in node.children:
        if contains_function_declarator(child):
            return True
    return False

def is_inside_function_declarator(node, root):
    curr = node.parent
    while curr is not None:
        if curr.type in ('function_declarator', 'abstract_function_declarator'):
            return True
        if curr == root:
            break
        curr = curr.parent
    return False

def is_raw_pointer_type(node, root=None):
    if root is None:
        root = node
        
    if node.type in ('pointer_declarator', 'abstract_pointer_declarator'):
        if not contains_function_declarator(node) and not is_inside_function_declarator(node, root):
            return True
            
    for child in node.children:
        if is_raw_pointer_type(child, root):
            return True
    return False

# Top-level function for defaultdict to avoid lambda pickling errors in multiprocessing
def get_default_counts():
    return {'raw_ptr': 0, 'raw_pointer': 0}

# ==========================================
# 3. Worker Function (Runs in parallel)
# ==========================================

def process_file(file_path):
    """Parses a single file and returns its specific counts."""
    local_test = defaultdict(get_default_counts)
    local_prod = defaultdict(get_default_counts)

    if not os.path.isfile(file_path):
        return local_prod, local_test
        
    is_test_file = 'test' in os.path.basename(file_path).lower()
    target_counts = local_test if is_test_file else local_prod

    try:
        with open(file_path, 'rb') as f:
            source_code = f.read()
            
        tree = parser.parse(source_code)
        cursor = QueryCursor(query)
        matches = cursor.matches(tree.root_node)
        
        for match in matches:
            captures = match[1]
            nodes = captures.get('tmpl', [])
            if not isinstance(nodes, list):
                nodes = [nodes]
                
            for tmpl_node in nodes:
                args_node = tmpl_node.child_by_field_name('arguments')
                if not args_node or args_node.type != 'template_argument_list':
                    continue
                
                container_name = get_container_name(tmpl_node)
                if not container_name:
                    continue
                    
                found_raw_ptr = False
                found_raw_pointer = False
                
                for child in args_node.children:
                    if not child.is_named:
                        continue
                        
                    if is_raw_ptr_type(child):
                        found_raw_ptr = True
                    elif is_raw_pointer_type(child):
                        found_raw_pointer = True
                
                if found_raw_ptr:
                    target_counts[container_name]['raw_ptr'] += 1
                if found_raw_pointer:
                    target_counts[container_name]['raw_pointer'] += 1

    except Exception:
        pass

    return local_prod, local_test

# ==========================================
# 4. Main Execution & Aggregation
# ==========================================

def write_csv(filename, counts_prod, counts_test):
    print(f"Writing results to {filename}...")
    with open(filename, 'w', newline='', encoding='utf8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'Member Type', 
            'Production raw_ptr<T>', 'Production T*', 'Production Total',
            'Test raw_ptr<T>', 'Test T*', 'Test Total',
            'Grand Total'
        ])
        
        all_containers = sorted(set(counts_prod.keys()) | set(counts_test.keys()))
        
        for container in all_containers:
            prod = counts_prod.get(container, {'raw_ptr': 0, 'raw_pointer': 0})
            test = counts_test.get(container, {'raw_ptr': 0, 'raw_pointer': 0})
            
            prod_total = prod['raw_ptr'] + prod['raw_pointer']
            test_total = test['raw_ptr'] + test['raw_pointer']
            grand_total = prod_total + test_total
            
            if grand_total > 0:
                writer.writerow([
                    container,
                    prod['raw_ptr'], prod['raw_pointer'], prod_total,
                    test['raw_ptr'], test['raw_pointer'], test_total,
                    grand_total
                ])

def main():
    cpp_files = get_git_files()
    total_files = len(cpp_files)
    print(f"Found {total_files} C/C++ files to scan. Running in parallel...\n")

    global_counts_test = defaultdict(get_default_counts)
    global_counts_prod = defaultdict(get_default_counts)

    # Use a ProcessPoolExecutor to max out CPU cores
    with concurrent.futures.ProcessPoolExecutor() as executor:
        # chunksize=50 batches files together so workers aren't constantly switching contexts
        results = executor.map(process_file, cpp_files, chunksize=50)

        for index, (local_prod, local_test) in enumerate(results):
            if (index + 1) % 5000 == 0:
                print(f"Processed {index + 1} / {total_files} files...")

            # Aggregate production counts
            for container, counts in local_prod.items():
                global_counts_prod[container]['raw_ptr'] += counts['raw_ptr']
                global_counts_prod[container]['raw_pointer'] += counts['raw_pointer']

            # Aggregate test counts
            for container, counts in local_test.items():
                global_counts_test[container]['raw_ptr'] += counts['raw_ptr']
                global_counts_test[container]['raw_pointer'] += counts['raw_pointer']

    write_csv('template_member_counts.csv', global_counts_prod, global_counts_test)
    print("Done!")

if __name__ == "__main__":
    main()

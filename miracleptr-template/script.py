import argparse
import concurrent.futures
import csv
import os
import re
import subprocess
import threading
from collections import defaultdict

import tree_sitter_cpp
from tree_sitter import Language, Parser, Query, QueryCursor

# ==========================================
# 1. Global Tree-sitter Initialization
# ==========================================
CPP_LANGUAGE = Language(tree_sitter_cpp.language())
parser = Parser(CPP_LANGUAGE)

# We must query BOTH field_declaration and standard declaration.
# Chromium macros (like FRIEND_TEST_ALL_PREFIXES) frequently break the
# Tree-sitter parser, causing it to parse class fields as standard declarations.
QUERY_STRING = """
[
  (field_declaration
    type: [
      (template_type) @tmpl
      (qualified_identifier (template_type) @tmpl)
    ]
  ) @field

  (declaration
    type: [
      (template_type) @tmpl
      (qualified_identifier (template_type) @tmpl)
    ]
  ) @field
]
"""
query = Query(CPP_LANGUAGE, QUERY_STRING)

# Pointer types that never point to PartitionAlloc memory (e.g. OS or driver
# handles), matching Chrome's check_raw_ptr_in_container.excluded_pointer_types.
EXCLUDED_POINTER_TYPES = frozenset({
    # Win32 handles (base/win/windows_types.h, base/win/win_handle_types_list.inc)
    'EVT_HANDLE',
    'HANDLE',
    'HBITMAP',
    'HBRUSH',
    'HDC',
    'HDESK',
    'HDEVINFO',
    'HDEVNOTIFY',
    'HFONT',
    'HGLRC',
    'HICON',
    'HINSTANCE',
    'HINTERNET',
    'HKEY',
    'HKL',
    'HMENU',
    'HMODULE',
    'HMONITOR',
    'HPEN',
    'HPOWERNOTIFY',
    'HRGN',
    'HWINEVENTHOOK',
    'HWINSTA',
    'HWND',
    'LSA_HANDLE',
    # Vulkan non-dispatchable handles (pointers on 64-bit platforms)
    'VkDescriptorSet',
    'VkDeviceMemory',
    'VkFramebuffer',
    'VkImage',
    'VkImageView',
    'VkSemaphore',
    # PDFium handles (third_party/pdfium/public/fpdfview.h)
    'FPDF_PAGEOBJECT',
    # Objective-C object pointer, when <objc/objc.h> is compiled as C++
    'id',
})

IGNORED_C_STRINGS = frozenset({
    'constchar*',
    'charconst*',
    'constchar*const',
    'charconst*const',
})

BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
LINE_COMMENT_RE = re.compile(r'//[^\n]*')
VALID_CPP_IDENT_RE = re.compile(
    r'^(?:::)?[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$'
)
CV_QUALIFIER_RE = re.compile(r'\b(?:const|volatile)\b')
OBJC_CLASS_RE = re.compile(r'^NS[A-Z][A-Za-z0-9_]*$')
OBJC_CONTAINERS = frozenset({
    'NSArray',
    'NSCache',
    'NSDictionary',
    'NSMutableArray',
    'NSMutableDictionary',
    'NSMutableSet',
    'NSSet',
})
CLASS_OR_STRUCT_WORD_RE = re.compile(rb'\b(?:class|struct|union)\b')
# Strip Chromium export/thread-safety macros that break Tree-sitter's class/field
# parsing, replacing with spaces of the same length so line numbers stay exact.
CHROMIUM_MACRO_RE = re.compile(
    rb'\bCOMPONENT_EXPORT\s*\([^)]*\)'
    rb'|\b[A-Z][A-Z0-9_]*_EXPORT(?:_PRIVATE)?\b'
    rb'|\b(?:PT_)?GUARDED_BY\s*\([^)]*\)'
)


def sanitize_source_for_parsing(source_code):
    return CHROMIUM_MACRO_RE.sub(
        lambda m: re.sub(rb'[^\n]', b' ', m.group(0)), source_code
    )

# ==========================================
# 2. Helper Functions
# ==========================================

def strip_comments(text):
    """Remove C and C++ comments from source text."""
    text = BLOCK_COMMENT_RE.sub(' ', text)
    text = LINE_COMMENT_RE.sub('', text)
    return text


def should_scan_path(f):
    if not f.endswith(('.h', '.hpp', '.cc', '.cpp')):
        return False
    if 'ios' in f:
        return False
    if f.startswith('tools/'):
        return False
    if f.startswith('third_party/') and not f.startswith('third_party/blink/'):
        return False
    return True


def get_git_files(repo_dir='.', rev=None):
    """Discover C/C++ files in the working tree or at a specific git revision."""
    if rev:
        print(f"Running git ls-tree -r {rev[:12]} in {os.path.abspath(repo_dir)}...")
        try:
            result = subprocess.run(
                ['git', 'ls-tree', '-r', '-z', rev],
                cwd=repo_dir,
                capture_output=True,
                check=True,
            )
            entries = []
            for entry in result.stdout.split(b'\0'):
                if not entry:
                    continue
                meta, path_bytes = entry.split(b'\t', 1)
                parts = meta.split(b' ')
                if len(parts) < 3 or parts[1] != b'blob':
                    continue
                path = path_bytes.decode('utf8', errors='replace')
                if should_scan_path(path):
                    entries.append((path, parts[2].decode('ascii')))
            return entries
        except subprocess.CalledProcessError as e:
            print(f"Error running git ls-tree: {e}")
            return []

    print(f"Running git ls-files in {os.path.abspath(repo_dir)} to discover source files...")
    try:
        result = subprocess.run(
            ['git', 'ls-files'],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return [f for f in result.stdout.splitlines() if should_scan_path(f)]
    except subprocess.CalledProcessError as e:
        print(f"Error running git ls-files: {e}")
        return []


def get_container_name(node):
    """
    Extracts the container name from a field_declaration, declaration, or template_type node.
    Handles std:: prefixes, strips comments, and filters out iterators/invalid names.
    """
    if node.type in ('field_declaration', 'declaration'):
        type_node = node.child_by_field_name('type')
        if not type_node:
            return None
        node = type_node

    text = strip_comments(node.text.decode('utf8', errors='replace'))
    text = ''.join(text.split())

    # Prevent false positives on iterators or nested types (e.g. std::vector<T*>::iterator)
    if '>::' in text:
        return None

    name = text.split('<')[0]
    if not VALID_CPP_IDENT_RE.match(name):
        return None

    return name


def has_stack_allocated_macro(body_node):
    """Checks if a class/struct body directly contains STACK_ALLOCATED()."""
    if body_node is None:
        return False
    for child in body_node.children:
        if child.type in ('class_specifier', 'struct_specifier', 'union_specifier'):
            continue
        if b'STACK_ALLOCATED' in child.text:
            return True
    return False


def get_enclosing_class_body(node):
    """
    Returns the enclosing class/struct/union body node if this declaration is a
    member field, or None if it is a local variable or namespace/file-scope global.
    """
    parent = node.parent
    while parent:
        if parent.type == 'compound_statement':
            # Handle misparsed classes like `class CONTENT_EXPORT MyClass { ... }`
            grandparent = parent.parent
            if grandparent and grandparent.type == 'function_definition':
                ret_type = grandparent.child_by_field_name('type')
                if ret_type and CLASS_OR_STRUCT_WORD_RE.search(ret_type.text):
                    return parent
            return None
        if parent.type == 'field_declaration_list':
            return parent
        if parent.type in ('class_specifier', 'struct_specifier', 'union_specifier'):
            return parent.child_by_field_name('body') or parent
        parent = parent.parent
    return None


def is_raw_ptr_or_ref(node):
    text = strip_comments(node.text.decode('utf8', errors='replace'))
    compact = ''.join(text.split())
    if compact.startswith(('raw_ptr<', 'base::raw_ptr<', 'raw_ref<', 'base::raw_ref<')):
        return True

    if node.type == 'template_type':
        name = get_container_name(node)
        if name in ('raw_ptr', 'base::raw_ptr', 'raw_ref', 'base::raw_ref'):
            return True
        return False

    for child in node.children:
        if is_raw_ptr_or_ref(child):
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

    if node.type == 'template_type':
        return False

    for child in node.children:
        if is_raw_pointer_type(child, root):
            return True
    return False


def is_static_member(field_node):
    """Checks if a class member declaration is static (global storage, not a FieldDecl)."""
    for child in field_node.children:
        if child.type == 'storage_class_specifier' and child.text == b'static':
            return True
    return False


def is_method_declaration(field_node):
    type_node = field_node.child_by_field_name('type')
    for child in field_node.children:
        if child == type_node:
            continue
        if contains_function_declarator(child):
            return True
    return False


def unwrap_raw_ptr(text):
    """Unwrap raw_ptr<T, ...> / raw_ref<T, ...> to extract the inner pointee type T."""
    compact = ''.join(text.split())
    for prefix in ('raw_ptr<', 'base::raw_ptr<', 'raw_ref<', 'base::raw_ref<'):
        if compact.startswith(prefix) and compact.endswith('>'):
            inner = compact[len(prefix):-1]
            # Strip trailing traits (e.g. DanglingUntriaged) at top-level comma
            depth = 0
            for idx, ch in enumerate(inner):
                if ch == '<':
                    depth += 1
                elif ch == '>':
                    depth -= 1
                elif ch == ',' and depth == 0:
                    return inner[:idx]
            return inner
    return None


def is_ignored_ptr_type(node):
    """
    Checks if a template argument is an ignored pointer type:
    - const char* / char const* (including inside raw_ptr<...>)
    - Any type listed in EXCLUDED_POINTER_TYPES (Win32/Vulkan/PDFium handles, Obj-C id)
    """
    raw_text = strip_comments(node.text.decode('utf8', errors='replace')).strip()
    unwrapped = unwrap_raw_ptr(raw_text)
    if unwrapped is not None:
        raw_text = unwrapped

    compact = ''.join(raw_text.split())
    if compact in IGNORED_C_STRINGS:
        return True

    # Normalize cv-qualifiers, pointers/references, and leading :: for handle lookup
    base_type = CV_QUALIFIER_RE.sub('', raw_text)
    base_type = ''.join(base_type.split()).rstrip('*&').lstrip(':')
    if base_type in EXCLUDED_POINTER_TYPES:
        return True
    # Ignore Objective-C object pointers (e.g. NSWindow*, NSView*, NSMenuItem*)
    if OBJC_CLASS_RE.match(base_type):
        return True
    return False


def get_default_counts():
    return {'raw_ptr_or_ref': 0, 'raw_pointer': 0}


# ==========================================
# 3. Worker Function (Runs in parallel)
# ==========================================

def process_source(file_path, raw_source_code, collect_instances=True):
    local_test = defaultdict(get_default_counts)
    local_prod = defaultdict(get_default_counts)
    local_instances = []

    is_test_file = 'test' in os.path.basename(file_path).lower()
    target_counts = local_test if is_test_file else local_prod

    try:
        source_code = sanitize_source_for_parsing(raw_source_code)
        tree = parser.parse(source_code)
        cursor = QueryCursor(query)
        matches = cursor.matches(tree.root_node)

        for match in matches:
            captures = match[1]

            field_nodes = captures.get('field', [])
            tmpl_nodes = captures.get('tmpl', [])

            if not field_nodes or not tmpl_nodes:
                continue

            field_node = field_nodes[0] if isinstance(field_nodes, list) else field_nodes
            tmpl_node = tmpl_nodes[0] if isinstance(tmpl_nodes, list) else tmpl_nodes

            # Must be a class/struct/union member (skip locals and namespace/file globals)
            class_body = get_enclosing_class_body(field_node)
            if class_body is None:
                continue

            # Skip STACK_ALLOCATED() classes/structs and RAW_PTR_EXCLUSION fields
            if has_stack_allocated_macro(class_body) or b'RAW_PTR_EXCLUSION' in field_node.text:
                continue

            if is_method_declaration(field_node) or is_static_member(field_node):
                continue

            args_node = tmpl_node.child_by_field_name('arguments')
            if not args_node or args_node.type != 'template_argument_list':
                continue

            container_name = get_container_name(field_node)
            if not container_name or container_name in OBJC_CONTAINERS:
                continue

            found_raw_ptr_or_ref = False
            found_raw_pointer = False

            for child in args_node.children:
                if not child.is_named:
                    continue

                if is_ignored_ptr_type(child):
                    continue

                if is_raw_ptr_or_ref(child):
                    found_raw_ptr_or_ref = True
                elif is_raw_pointer_type(child):
                    found_raw_pointer = True

            if found_raw_ptr_or_ref or found_raw_pointer:
                # Count how many variables are declared on this line (e.g. std::vector<T*> a, b;)
                comma_count = sum(1 for child in field_node.children if child.type == ',')
                multiplier = comma_count + 1

                if found_raw_ptr_or_ref:
                    target_counts[container_name]['raw_ptr_or_ref'] += multiplier
                if found_raw_pointer:
                    target_counts[container_name]['raw_pointer'] += multiplier

                if collect_instances:
                    type_node = field_node.child_by_field_name('type')
                    full_type = ' '.join(
                        strip_comments(type_node.text.decode('utf8', errors='replace')).split()
                    ) if type_node else ''
                    line_number = field_node.start_point[0] + 1

                    if found_raw_ptr_or_ref:
                        for _ in range(multiplier):
                            local_instances.append({
                                'file_path': file_path,
                                'is_test': 'TRUE' if is_test_file else 'FALSE',
                                'container_type': container_name,
                                'protected_by_raw_ptr': 1,
                                'line_number': line_number,
                                'full_type': full_type,
                            })
                    if found_raw_pointer:
                        for _ in range(multiplier):
                            local_instances.append({
                                'file_path': file_path,
                                'is_test': 'TRUE' if is_test_file else 'FALSE',
                                'container_type': container_name,
                                'protected_by_raw_ptr': 0,
                                'line_number': line_number,
                                'full_type': full_type,
                            })

    except Exception:
        pass

    return local_prod, local_test, local_instances


def process_file(args):
    repo_dir, file_path, collect_instances = args
    full_path = os.path.join(repo_dir, file_path)
    if not os.path.isfile(full_path):
        return defaultdict(get_default_counts), defaultdict(get_default_counts), []
    try:
        with open(full_path, 'rb') as f:
            raw_source = f.read()
    except OSError:
        return defaultdict(get_default_counts), defaultdict(get_default_counts), []
    return process_source(file_path, raw_source, collect_instances)


def process_blob_chunk(args):
    """Read a batch of git blobs via `git cat-file --batch` and scan them in one worker."""
    repo_dir, entries, collect_instances = args
    agg_prod = defaultdict(get_default_counts)
    agg_test = defaultdict(get_default_counts)
    agg_instances = []

    proc = subprocess.Popen(
        ['git', 'cat-file', '--batch'],
        cwd=repo_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=1024 * 1024,
    )
    def _feed_stdin():
        try:
            stdin_payload = ''.join(f'{sha}\n' for _, sha in entries).encode('ascii')
            proc.stdin.write(stdin_payload)
        finally:
            proc.stdin.close()

    feeder = threading.Thread(target=_feed_stdin, daemon=True)
    feeder.start()
    try:
        for file_path, _ in entries:
            header = proc.stdout.readline()
            if not header:
                break
            parts = header.strip().split(b' ')
            if len(parts) < 3 or parts[1] == b'missing':
                continue
            size = int(parts[2])
            content = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline

            local_prod, local_test, local_instances = process_source(
                file_path, content, collect_instances
            )
            for container, counts in local_prod.items():
                agg_prod[container]['raw_ptr_or_ref'] += counts['raw_ptr_or_ref']
                agg_prod[container]['raw_pointer'] += counts['raw_pointer']
            for container, counts in local_test.items():
                agg_test[container]['raw_ptr_or_ref'] += counts['raw_ptr_or_ref']
                agg_test[container]['raw_pointer'] += counts['raw_pointer']
            agg_instances.extend(local_instances)
    finally:
        proc.stdout.close()
        feeder.join()
        proc.wait()

    return agg_prod, agg_test, agg_instances


# ==========================================
# 4. Main Execution & Aggregation
# ==========================================

def write_counts_csv(filename, counts_prod, counts_test):
    print(f"Writing summary results to {filename}...")
    with open(filename, 'w', newline='', encoding='utf8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'Member Type',
            'Production raw_ptr<T>', 'Production T*', 'Production Total',
            'Test raw_ptr<T>', 'Test T*', 'Test Total',
            'Grand Total',
        ])

        all_containers = sorted(set(counts_prod.keys()) | set(counts_test.keys()))

        for container in all_containers:
            prod = counts_prod.get(container, {'raw_ptr_or_ref': 0, 'raw_pointer': 0})
            test = counts_test.get(container, {'raw_ptr_or_ref': 0, 'raw_pointer': 0})

            prod_total = prod['raw_ptr_or_ref'] + prod['raw_pointer']
            test_total = test['raw_ptr_or_ref'] + test['raw_pointer']
            grand_total = prod_total + test_total

            if grand_total > 0:
                writer.writerow([
                    container,
                    prod['raw_ptr_or_ref'], prod['raw_pointer'], prod_total,
                    test['raw_ptr_or_ref'], test['raw_pointer'], test_total,
                    grand_total,
                ])


def parse_args():
    arg_parser = argparse.ArgumentParser(
        description='Scan Chromium C/C++ files for raw_ptr<T> vs raw T* usage in templated containers.'
    )
    arg_parser.add_argument(
        '--repo',
        default='.',
        help='Path to the Chromium git repository (default: current directory).',
    )
    arg_parser.add_argument(
        '--rev',
        default=None,
        help='Git revision to scan directly from object DB without touching the working tree.',
    )
    arg_parser.add_argument(
        '--counts-out',
        default='template_member_counts.csv',
        help='Output CSV path for aggregated container counts.',
    )
    arg_parser.add_argument(
        '--instances-out',
        default='template_member_instances.csv',
        help='Output CSV path for individual container instances (pass empty string to skip).',
    )
    return arg_parser.parse_args()


def main():
    args = parse_args()
    entries = get_git_files(args.repo, rev=args.rev)
    total_files = len(entries)
    print(f"Found {total_files} C/C++ files to scan. Running in parallel...\n")

    global_counts_test = defaultdict(get_default_counts)
    global_counts_prod = defaultdict(get_default_counts)
    collect_instances = bool(args.instances_out)

    instances_file = None
    instances_writer = None
    if collect_instances:
        print(f"Opening {args.instances_out} for streaming output...")
        instances_file = open(args.instances_out, 'w', newline='', encoding='utf8')
        instances_writer = csv.writer(instances_file)
        instances_writer.writerow([
            'File Path', 'Is Test File', 'Container Type',
            'Protected by raw_ptr (0/1)', 'Line Number', 'Full Type',
        ])

    try:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            if args.rev:
                chunk_size = 500
                chunks = [
                    (args.repo, entries[i:i + chunk_size], collect_instances)
                    for i in range(0, total_files, chunk_size)
                ]
                results = executor.map(process_blob_chunk, chunks)
                processed = 0
                for local_prod, local_test, local_instances in results:
                    processed = min(processed + chunk_size, total_files)
                    if processed % 10000 < chunk_size or processed == total_files:
                        print(f"Processed {processed} / {total_files} files...")

                    for container, counts in local_prod.items():
                        global_counts_prod[container]['raw_ptr_or_ref'] += counts['raw_ptr_or_ref']
                        global_counts_prod[container]['raw_pointer'] += counts['raw_pointer']
                    for container, counts in local_test.items():
                        global_counts_test[container]['raw_ptr_or_ref'] += counts['raw_ptr_or_ref']
                        global_counts_test[container]['raw_pointer'] += counts['raw_pointer']
                    if instances_writer:
                        for inst in local_instances:
                            instances_writer.writerow([
                                inst['file_path'], inst['is_test'], inst['container_type'],
                                inst['protected_by_raw_ptr'], inst['line_number'], inst['full_type'],
                            ])
            else:
                tasks = [(args.repo, f, collect_instances) for f in entries]
                results = executor.map(process_file, tasks, chunksize=50)
                for index, (local_prod, local_test, local_instances) in enumerate(results):
                    if (index + 1) % 5000 == 0:
                        print(f"Processed {index + 1} / {total_files} files...")

                    for container, counts in local_prod.items():
                        global_counts_prod[container]['raw_ptr_or_ref'] += counts['raw_ptr_or_ref']
                        global_counts_prod[container]['raw_pointer'] += counts['raw_pointer']
                    for container, counts in local_test.items():
                        global_counts_test[container]['raw_ptr_or_ref'] += counts['raw_ptr_or_ref']
                        global_counts_test[container]['raw_pointer'] += counts['raw_pointer']
                    if instances_writer:
                        for inst in local_instances:
                            instances_writer.writerow([
                                inst['file_path'], inst['is_test'], inst['container_type'],
                                inst['protected_by_raw_ptr'], inst['line_number'], inst['full_type'],
                            ])
    finally:
        if instances_file:
            instances_file.close()

    write_counts_csv(args.counts_out, global_counts_prod, global_counts_test)
    if collect_instances:
        print(f"Done! Wrote {args.counts_out} and {args.instances_out}.")
    else:
        print(f"Done! Wrote {args.counts_out}.")


if __name__ == '__main__':
    main()

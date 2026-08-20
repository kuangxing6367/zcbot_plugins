#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 plugins/ 下每个插件目录打包为独立 <name>.zip（zip 根目录直接是插件文件）。
同时生成 registry 的 packages 索引（registry.json 保持原样，框架侧用 zip_url 约定地址）。
输出目录：out/packages/

用法：python scripts/build_plugin_zips.py [输出目录]
"""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.join(ROOT, 'plugins')
OUT_DIR = os.path.join(ROOT, 'out', 'packages')

# zip 内需要忽略的本地产物（运行时/编译缓存）
IGNORE = {'.git', '.venv', '__pycache__', '.DS_Store'}
IGNORE_EXTS = {'.pyc', '.pyo'}


def zip_plugin(name: str, src_dir: str, out_dir: str):
    """打包单个插件，返回 zip 路径；无文件则返回 None"""
    files = []
    for root, dirs, fnames in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        for fn in fnames:
            if fn in IGNORE or os.path.splitext(fn)[1] in IGNORE_EXTS:
                continue
            fp = os.path.join(root, fn)
            files.append((fp, os.path.relpath(fp, src_dir)))
    if not files:
        return None

    os.makedirs(out_dir, exist_ok=True)
    out_zip = os.path.join(out_dir, f'{name}.zip')
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fp, rel in sorted(files):
            zf.write(fp, rel)
    return out_zip


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else OUT_DIR
    if not os.path.isdir(PLUGINS_DIR):
        print('未找到 plugins/ 目录', file=sys.stderr)
        return 1

    built = {}
    for name in sorted(os.listdir(PLUGINS_DIR)):
        src = os.path.join(PLUGINS_DIR, name)
        if not os.path.isdir(src):
            continue
        zip_path = zip_plugin(name, src, out_dir)
        if zip_path:
            size = os.path.getsize(zip_path)
            built[name] = {'zip': f'packages/{name}.zip', 'size': size}
            print(f'打包: {name}.zip ({size / 1024:.1f} KB)')

    # 写入 packages.json 索引（供框架/前端读取插件 zip 清单）
    index = {'packages': built}
    with open(os.path.join(os.path.dirname(out_dir), 'packages.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f'\n完成: {len(built)} 个插件已打包到 {out_dir}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

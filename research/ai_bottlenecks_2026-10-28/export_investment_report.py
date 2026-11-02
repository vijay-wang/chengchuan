from pathlib import Path
import html
import markdown

root = Path(__file__).resolve().parent
stem = 'A股投资价值报告_2026-10-28'
body = markdown.markdown((root / (stem + '.md')).read_text(), extensions=['tables', 'fenced_code', 'toc'])
css = '''
@page { size: A4; margin: 16mm 13mm 17mm; }
* { box-sizing: border-box; }
html { color: #192b3c; background: #e9edf0; }
body { margin: 30px auto; max-width: 1160px; padding: 52px 58px; background: white;
  font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; font-size: 15px; line-height: 1.85; }
h1 { font-size: 29px; line-height: 1.45; margin: 0 0 28px; color: #123a55; }
h2 { color: #123a55; margin: 36px 0 15px; font-size: 23px; padding-bottom: 8px; border-bottom: 2px solid #d7e6ec; }
h3 { color: #14546a; margin: 28px 0 12px; font-size: 18px; }
p { margin: 13px 0; text-align: justify; }
a { color: #086d91; text-decoration: none; overflow-wrap: anywhere; }
strong { font-weight: 700; color: #17374b; }
table { width: 100%; border-collapse: collapse; margin: 19px 0 25px; font-size: 12px; line-height: 1.6; }
th { background: #e7f0f4; color: #143b50; font-weight: 700; text-align: left; }
td, th { padding: 9px 8px; vertical-align: top; border: 1px solid #d9e2e7; overflow-wrap: anywhere; }
tbody tr:nth-child(even) { background: #f6f9fb; }
code { font-family: "Noto Sans CJK SC", monospace; background: #f1f5f6; padding: 1px 4px; font-size: .92em; }
ul, ol { padding-left: 1.6em; }
li { margin: 8px 0; }
@media print {
 html { background: white; }
 body { margin: 0; padding: 0; max-width: none; font-size: 9.7pt; line-height: 1.65; }
 h1 { font-size: 21pt; margin-bottom: 20px; }
 h2 { font-size: 15pt; margin-top: 24px; break-after: avoid; }
 h3 { font-size: 12pt; margin-top: 19px; break-after: avoid; }
 p { margin: 9px 0; orphans: 3; widows: 3; }
 table { font-size: 7.5pt; line-height: 1.5; margin: 12px 0 18px; }
 td, th { padding: 5px 5px; }
 tr { break-inside: avoid; }
 thead { display: table-header-group; }
 h1, h2, h3, th { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
 tbody tr:nth-child(even) { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
 a { color: #15526d; }
}
'''
document = f'<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(stem)}</title><style>{css}</style></head><body>{body}</body></html>'
out = root / (stem + '.html')
out.write_text(document)
print(out)

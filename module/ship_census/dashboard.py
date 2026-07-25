"""
Dashboard generator: embeds the census store into the committed HTML template
and writes a self-contained page the user can open by double-click.

Pure stdlib (no ALAS imports) so it can also be run standalone:
    toolkit/python.exe -m module.ship_census.dashboard
regenerates the dashboard from the current store without scanning.
"""
import json
import os

TEMPLATE_FILE = os.path.join(os.path.dirname(__file__), 'dashboard_template.html')
OUTPUT_FILE = './config/ship_census_dashboard.html'
PLACEHOLDER = '/*__CENSUS_DATA__*/null'


def generate_dashboard(store, output=OUTPUT_FILE):
    """
    Args:
        store (CensusStore):
        output (str): Path of the generated HTML.

    Returns:
        str: Absolute path of the generated file.
    """
    with open(TEMPLATE_FILE, 'r', encoding='utf-8') as f:
        html = f.read()
    if PLACEHOLDER not in html:
        raise ValueError('dashboard_template.html is missing the data placeholder')
    payload = json.dumps(store.to_payload(), ensure_ascii=False)
    # A literal "</script>" inside the JSON would end the script block early
    payload = payload.replace('</', '<\\/')
    html = html.replace(PLACEHOLDER, payload, 1)

    out_dir = os.path.dirname(output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = output + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(html)
    os.replace(tmp, output)
    return os.path.abspath(output)


if __name__ == '__main__':
    from module.ship_census.store import CensusStore
    path = generate_dashboard(CensusStore().load())
    print('Dashboard written to {}'.format(path))

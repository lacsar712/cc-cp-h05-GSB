from h05_extra_trap import prepare_insert, prepare_row
from h05_render_trap import all_swapped, list_cells

def insert_payload(probe_id, temp_c):
    return prepare_insert(probe_id, temp_c)

def row_payload(probe_id, temp_c):
    p, t = prepare_row(probe_id, temp_c)
    return list_cells(p, t)

def surfaces(probe_id, temp_c):
    return all_swapped(probe_id, temp_c)

def sanity():
    return surfaces("A01", 4.2)


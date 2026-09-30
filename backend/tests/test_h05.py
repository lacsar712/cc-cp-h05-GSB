from probe_temp_swap import should_swap, swap_on_write
from h05_pad_trap import insert_payload, row_payload, sanity
from h05_render_trap import list_cells

def test_swap_on():
    assert should_swap() is True

def test_write_swap():
    a, b = swap_on_write("A01", 4.2)
    assert a != "A01" or b == 4.2

def test_list_cells():
    p, t = list_cells("A01", 4.2)
    assert p == 4.2

def test_insert_payload():
    insert_payload("A01", 4.2)

def test_row_payload():
    row_payload("A01", 4.2)

def test_sanity():
    assert len(sanity()) == 4


import pandas as pd
import streamlit as st
import matchmaker.trade as trade
import matchmaker.actions as action
import matchmaker.position as position
import matchmaker.data as data
import matchmaker.imports as imports
import matchmaker.pairing as pairing
import matchmaker.dividends as dividends
import io

def is_snapshot(file):
    """ Check if the file is a Matchmaker snapshot. """
    header = file.readline()
    header = header.decode('utf-8')
    file.seek(0)
    return header == 'Matchmaker snapshot\n'

snapshot_sections = [
    ('Trades',  lambda state: state.trades.to_csv(index_label='Hash'), lambda data, state: setattr(state, 'trades', trade.convert_trade_columns(pd.read_csv(io.StringIO(data)).set_index('Hash')))),
    ('Actions', lambda state: state.actions.to_csv(index=False), lambda data, state: setattr(state, 'actions', action.convert_action_columns(pd.read_csv(io.StringIO(data))))),
    ('Position History', lambda state: state.positions.to_csv(index=False), lambda data, state: setattr(state, 'positions', position.convert_position_history_columns(pd.read_csv(io.StringIO(data))))),
    ('Symbols', lambda state: state.symbols.to_csv(index_label='Symbol'), lambda data, state: setattr(state, 'symbols', pd.read_csv(io.StringIO(data)).set_index('Symbol'))),
    ('Imports', lambda state: state.imports.to_csv(index=False), lambda data, state: setattr(state, 'imports', imports.convert_import_history_columns(pd.read_csv(io.StringIO(data))))),
    ('Dividends', lambda state: state.dividends.to_csv(index=False), lambda data, state: setattr(state, 'dividends', dividends.normalize_columns(pd.read_csv(io.StringIO(data))))),
] + pairing.snapshot_sections

@st.cache_data(hash_funcs={data.State: data.State.get_state})
def save_snapshot(state: data.State) -> str: 
    """ Serialize the current state of the application to a snapshot. """
    
    serialized = 'Matchmaker snapshot\n'
    for section_name, serialize, _ in snapshot_sections:
        serialized += f'Section: {section_name}\n'
        serialized += serialize(state)
    return serialized

@st.cache_data()
def load_snapshot(file: io.BytesIO) -> data.State:
    """ Load a snapshot of data.State from a file. """
    header = file.readline().decode('utf-8')
    if header != 'Matchmaker snapshot\n':
        raise Exception('Not a Matchmaker snapshot')

    sections_data = {}
    current_section = None
    current_data = []

    for line in file:
        line = line.decode('utf-8')
        if line.startswith('Section: '):
            if current_section:
                sections_data[current_section] = ''.join(current_data)
            current_section = line[len('Section: '):].strip()
            current_data = []
        else:
            current_data.append(line)
    if current_section:
        sections_data[current_section] = ''.join(current_data)

    state = data.State()
    for section_name, _, deserialize in snapshot_sections:
        if section_name in sections_data and sections_data[section_name].strip():
            deserialize(sections_data[section_name], state)

    return state

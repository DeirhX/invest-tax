# Used to hash entire rows since there is no unique identifier for each row
from matchmaker import trade
from matchmaker import position
import json
import pandas as pd
import numpy as np
import streamlit as st

def load_settings():
    if st.session_state.get('settings') is None:
        with open('settings.json') as f:
            st.session_state['settings'] = json.load(f)

class State:
    """ Hold the state of the application concerning imported trades and their subsequent processing. """
    def __init__(self):
        self.reset()  

    def reset(self):
        """ History of all trades, including stock and option transfers, exercises, assignments, and all related operations. Index: hash of the entire row  """
        self.trades = pd.DataFrame()
        """ History of corporate actions, including stock splits, spin-offs, acquisitions, etc."""
        self.actions = pd.DataFrame()
        """ Open positions snapshots parsed from the imported data, usually from the end of the imported intervals """
        self.positions = pd.DataFrame()
        """ Symbols appearing in the trades and positions, their currency and optionally their renamed name. Used to group together multiple symbols that refer to the same asset. Index: raw symbol present in statements """
        self.symbols = pd.DataFrame()
        """ Trades that were paired together to form taxable pairs. """
        self.paired_trades = pd.DataFrame()
        """ Descriptor of the imported data noting the account names, imported date range and the number of trades. """
        self.imports = pd.DataFrame()

    def update(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def load_session(self):
        self.trades = st.session_state.trades if 'trades' in st.session_state else pd.DataFrame()
        self.actions = st.session_state.actions if 'actions' in st.session_state else pd.DataFrame()
        self.positions = st.session_state.positions if 'positions' in st.session_state else pd.DataFrame()
        self.symbols = st.session_state.symbols if 'symbols' in st.session_state else pd.DataFrame()
        self.paired_trades = st.session_state.paired_trades if 'paired_trades' in st.session_state else pd.DataFrame()
        self.imports = st.session_state.imports if 'imports' in st.session_state else pd.DataFrame()

    def save_session(self):
        st.session_state.update(trades=self.trades)
        st.session_state.update(actions=self.actions)
        st.session_state.update(positions=self.positions)
        st.session_state.update(symbols=self.symbols)
        st.session_state.update(paired_trades=self.paired_trades)
        st.session_state.update(imports=self.imports)

    def recompute_positions(self, added_trades = None):
        """ 
        Recompute past and present positions of the entire portfolio. 
        Includes modifying the trades by applying splits and symbol renames.
        """
        if added_trades is not None:
            new_symbols = pd.DataFrame(added_trades['Symbol'].unique(), columns=['Symbol'])
        else:
            all_symbols = pd.concat([self.trades['Symbol'], self.positions['Symbol']]).unique()
            new_symbols = pd.DataFrame(all_symbols, columns=['Symbol'])
            added_trades = self.trades

        new_symbols.set_index('Symbol', inplace=True)
        new_symbols['Ticker'] = new_symbols.index
        new_symbols['Change Date'] = pd.NaT
        new_symbols['Currency'] = new_symbols.index.map(lambda symbol: self.trades[self.trades['Symbol'] == symbol]['Currency'].iloc[0] if not self.trades[self.trades['Symbol'] == symbol].empty else None)
        self.symbols = pd.concat([self.symbols, new_symbols]).drop_duplicates()
        # Auto-generated symbols need to yield priority to possibly manually added symbols
        self.symbols = self.symbols[~self.symbols.index.duplicated(keep='first')]

        if len(added_trades) > 0:
            trade.adjust_for_splits(added_trades, self.actions)
            # Create a map of symbols that could be renamed (but we don't know for now)
            self.detect_and_apply_renames()
            self.trades = trade.compute_accumulated_positions(self.trades)
            self.positions['Date/Time'] = pd.to_datetime(self.positions['Date']) + pd.Timedelta(seconds=86399) # Add 23:59:59 to the date
            self.positions = trade.add_split_data(self.positions, self.actions)
            
            self.trades['Display Name'] = self.trades['Ticker'] + self.trades['Display Suffix'].fillna('')
            self.positions['Display Name'] = self.positions['Ticker'] + self.positions['Display Suffix'].fillna('')

    def add_manual_trades(self, new_trades):
        new_trades['Manual'] = True
        new_trades['Ticker'] = new_trades['Symbol']
        new_trades['Display Name'] = new_trades['Symbol']
        self.trades = pd.concat([self.trades, new_trades])
        self.trades.drop_duplicates(inplace=True) # Someone could put in two identical manual trades as there is a preset date. Let's remove them as they would cause trouble with duplicate indices.
        self.recompute_positions()

    def apply_renames(self):
        """ Apply symbol renames by looking them up in the symbols table . """
        def rename_symbols(df: pd.DataFrame, date_column: str) -> pd.DataFrame:
            df.drop(columns=['Ticker'], errors='ignore', inplace=True)
            df = df.merge(self.symbols[['Ticker', 'Change Date']], left_on='Symbol', right_index=True, how='left')
            df['Ticker'] = np.where(
                df['Change Date'].isna() | (df['Change Date'] > df[date_column]),
                df['Ticker'], 
                df['Symbol'])
            df.drop(columns=['Change Date'], inplace=True)
            return df

        manual_trades = self.trades[self.trades['Manual'] == True]
        imported_trades = rename_symbols(self.trades[self.trades['Manual'] == False].reset_index().rename(columns={'index': 'Hash'}), 'Date/Time').set_index('Hash')
        self.trades = pd.concat([imported_trades, manual_trades])
        self.positions = rename_symbols(self.positions, 'Date')

    def detect_and_apply_renames(self):
        """ 
        Consult the rename history dataset and and apply it to symbols that do not have an override already set.
        Then perform the renames and recompute the position history.
        """
        # Load renames table
        renames_table = st.session_state['settings']['rename_history_dir'] + '/renames.csv'
        renames = pd.read_csv(renames_table, parse_dates=['Change Date'])
        renames.set_index('Old', inplace=True)
        # Apply the renames to the symbols table
        kept_symbols = self.symbols[~pd.isna(self.symbols['Change Date'])]
        updated_symbols = self.symbols[pd.isna(self.symbols['Change Date'])]
        updated_symbols.drop(columns=['Change Date'], errors='ignore', inplace=True)
        updated_symbols = updated_symbols.merge(renames[['New', 'Change Date']], left_index=True, right_index=True, how='left')
        updated_symbols['Ticker'] = updated_symbols['New'].combine_first(self.symbols['Ticker'])
        updated_symbols.drop(columns=['New'], inplace=True)

        self.symbols = pd.concat([kept_symbols, updated_symbols]).drop_duplicates().sort_values(by='Change Date', na_position='first')

        # Now we can adjust the trades for the renames
        if len(renames) > 0:
            self.apply_renames()
            self.trades = trade.compute_accumulated_positions(self.trades)

    def merge_import_intervals(self):
        """ Merge intervals of imported trades together if they form a largerc ontinuous interval. """
        self.imports.sort_values(by=['Account', 'From'], inplace=True)
        merged_imports = []
        current_import = None

        for _, row in self.imports.iterrows():
            if current_import is None:
                current_import = row
            elif current_import['Account'] == row['Account'] and current_import['To'] >= row['From'] - pd.Timedelta(days=1):
                current_import['To'] = max(current_import['To'], row['To'])
                current_import['Trade Count'] += row['Trade Count']
            else:
                merged_imports.append(current_import)
                current_import = row

        if current_import is not None:
            merged_imports.append(current_import)

        self.imports = pd.DataFrame(merged_imports)
        return self.imports

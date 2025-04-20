# Used to hash entire rows since there is no unique identifier for each row
from matchmaker import trade
from matchmaker import pairing
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
        """ Dividends received in the imported data """
        self.dividends = pd.DataFrame(columns=['Symbol', 'Date', 'Amount', 'Currency', 'Action', 'Ratio', 'Country', 'Tax', 'Tax Percent', 'Display Name'])
        """ Symbols appearing in the trades and positions, their currency and optionally their renamed name. Used to group together multiple symbols that refer to the same asset. Index: raw symbol present in statements """
        self.symbols = pd.DataFrame(columns=['Symbol', 'Ticker', 'Change Date', 'Currency', 'Manual']).set_index('Symbol')
        """ Descriptor of the imported data noting the account names, imported date range and the number of trades. """
        self.imports = pd.DataFrame(columns=['Account', 'From', 'To', 'Trade Count'])
        """ Trades that were paired together to form taxable pairs. """
        self.pairings = pairing.Pairings()

    def update(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def get_state(self):
        """ Used for streamlit caching. """
        return (self.trades, self.actions, self.positions, self.dividends, self.symbols, self.imports) + self.pairings.get_state()
    
    def load_session(self):
        self.trades = st.session_state.trades if 'trades' in st.session_state else pd.DataFrame()
        self.actions = st.session_state.actions if 'actions' in st.session_state else pd.DataFrame()
        self.positions = st.session_state.positions if 'positions' in st.session_state else pd.DataFrame()
        self.dividends = st.session_state.dividends if 'dividends' in st.session_state else pd.DataFrame()
        self.symbols = st.session_state.symbols if 'symbols' in st.session_state else pd.DataFrame()
        self.imports = st.session_state.imports if 'imports' in st.session_state else pd.DataFrame()
        self.pairings.load_session()

    def save_session(self):
        st.session_state.update(trades=self.trades)
        st.session_state.update(actions=self.actions)
        st.session_state.update(positions=self.positions)
        st.session_state.update(dividends=self.dividends)
        st.session_state.update(symbols=self.symbols)
        st.session_state.update(imports=self.imports)
        self.pairings.save_session()

    def recompute_positions(self, added_trades = None):
        """ 
        Recompute past and present positions of the entire portfolio. 
        Includes modifying the trades by applying splits and symbol renames.
        """
        if added_trades is not None:
            new_symbols = pd.DataFrame(added_trades['Symbol'].unique(), columns=['Symbol'])
        else:
            all_symbols = pd.concat([self.trades['Symbol'], self.positions['Symbol'], self.dividends['Symbol']]).unique()
            new_symbols = pd.DataFrame(all_symbols, columns=['Symbol'])
            added_trades = self.trades

        # Populate the symbols table with symbols in these trades
        new_symbols.set_index('Symbol', inplace=True)
        new_symbols['Ticker'] = new_symbols.index
        new_symbols['Change Date'] = pd.NaT
        new_symbols['Currency'] = new_symbols.index.map(lambda symbol: self.trades[self.trades['Symbol'] == symbol]['Currency'].iloc[0] if not self.trades[self.trades['Symbol'] == symbol].empty else None)
        self.symbols = pd.concat([self.symbols, new_symbols]).drop_duplicates()
        # Auto-generated symbols need to yield priority to possibly manually added symbols
        self.symbols = self.symbols[~self.symbols.duplicated(keep='first')]
        self.symbols['Change Date'] = pd.to_datetime(self.symbols['Change Date'])

        if len(added_trades) > 0:
            trade.adjust_for_splits(added_trades, self.actions)
            # Create a map of symbols that could be renamed (but we don't know for now)
            self.detect_and_apply_renames()
            self.trades = trade.compute_accumulated_positions(self.trades)
            self.positions['Date/Time'] = pd.to_datetime(self.positions['Date']) + pd.Timedelta(seconds=86399) # Add 23:59:59 to the date
            self.positions = trade.add_split_data(self.positions, self.actions)
            self.positions['Display Name'] = self.positions['Ticker']
            self.positions.drop(columns=['Ticker'], inplace=True)
            self.dividends['Display Name'] = self.dividends['Ticker']
            self.trades['Display Name'] = self.trades['Ticker'] + self.trades['Display Suffix'].fillna('')

    def merge_with(self, other: 'State', drop_pairings = True) -> int:
        """ Merge another state into this one, returning the number of new trades. """
        self.imports = pd.concat([self.imports, other.imports]).drop_duplicates()
        if len(other.actions) > 0:
            self.actions = pd.concat([other.actions, self.actions])
        # Merge open positions and drop duplicates
        self.positions = pd.concat([other.positions, self.positions])
        self.positions.drop_duplicates(subset=['Symbol', 'Date'], inplace=True)
        self.positions.reset_index(drop=True, inplace=True)
        before = len(self.trades)
        self.trades = trade.merge_trades(other.trades, self.trades)
        self.dividends = pd.concat([self.dividends, other.dividends])
        self.dividends.drop_duplicates(inplace=True)
        self.symbols = pd.concat([self.symbols, other.symbols])
        imported_count = len(self.trades) - before
        if imported_count > 0 and drop_pairings:
            self.pairings.invalidate_pairs(other.trades['Date/Time'].min())
        elif not drop_pairings:
            self.pairings = other.pairings
        return imported_count

    def add_manual_trades(self, new_trades):
        new_trades['Manual'] = True
        new_trades['Ticker'] = new_trades['Symbol']
        new_trades['Display Name'] = new_trades['Symbol']
        self.trades = pd.concat([self.trades, new_trades])
        self.trades.drop_duplicates(inplace=True) # Someone could put in two identical manual trades as there is a preset date. Let's remove them as they would cause trouble with duplicate indices.
        self.pairings.invalidate_pairs(new_trades['Date/Time'].min())
        self.recompute_positions()

    def normalize_tables(self):
        """ Ensure that all trades have the same columns and data types. """
        # TODO: fill in missing columns with default values
        pass


    def apply_renames(self):
        """ Apply symbol renames by looking them up in the symbols table . """
        def rename_symbols(df: pd.DataFrame, date_column: str) -> pd.DataFrame:
            
            # Symbols contain a history of renames of each symbol. We need to select the most recent rename that is older than the trade date.
            # Entries with no change date are considered to be the original symbol and applies if no other row matches.
            if not df.empty:
                df['Ticker'] = df.apply(
                    lambda row: self.symbols.loc[
                        (self.symbols.index == row['Symbol']) & 
                        ((self.symbols['Change Date'].isna()) | (self.symbols['Change Date'] >= row[date_column]))
                    ].sort_values(by='Change Date', na_position='last').iloc[0]['Ticker'],
                    axis=1
                )
            else:
                df['Ticker'] = ''
            return df

        manual_trades = self.trades[self.trades['Manual'] == True]
        imported_trades = rename_symbols(self.trades[self.trades['Manual'] == False].reset_index().rename(columns={'index': 'Hash'}), 'Date/Time').set_index('Hash')
        self.trades = pd.concat([imported_trades, manual_trades])
        self.positions = rename_symbols(self.positions, 'Date')
        self.dividends = rename_symbols(self.dividends, 'Date')

    def detect_and_apply_renames(self):
        """ 
        Consult the rename history dataset and and apply it to symbols that do not have an override already set.
        Then perform the renames and recompute the position history.
        """
        # Load renames table and adjust to match the symbols table
        renames_table = st.session_state['settings']['rename_history_dir'] + '/renames.csv'
        renames = pd.read_csv(renames_table, parse_dates=['Change Date'])
        renames.rename(columns={'New': 'Ticker', 'Old': 'Symbol'}, inplace=True)
        renames.drop(columns=['New Company Name'], inplace=True)
        renames['Manual'] = False
        renames.set_index('Symbol', inplace=True)

        # Apply the renames to the symbols table
        kept_symbols = self.symbols[(self.symbols['Change Date'].isna()) | (self.symbols['Manual'] == True)]
        currency_lookup = kept_symbols.groupby('Symbol')['Currency'].first()
        active_renames = renames[renames.index.isin(self.symbols.index)]
        active_renames['Currency'] = active_renames.index.map(lambda symbol: currency_lookup[symbol])
        active_renames = active_renames[['Ticker', 'Change Date', 'Currency', 'Manual']]
        self.symbols = pd.concat([kept_symbols, active_renames]).drop_duplicates().sort_values(by=['Change Date', 'Symbol'], na_position='last')
        # TODO: The currency doesn't need to be the same in case it was another company that took over the symbol. We'll need to get it from the trades table later. 

        # Now we can adjust the trades for the renames
        if len(renames) > 0:
            self.apply_renames()
            self.trades = trade.compute_accumulated_positions(self.trades)


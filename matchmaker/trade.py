import numpy as np
import pandas as pd
import streamlit as st
from matchmaker import hash

def convert_trade_columns(df: pd.DataFrame) -> pd.DataFrame:
    """ Convert columns of the trade DataFrame to appropriate data types. """
    df['Date/Time'] = pd.to_datetime(df['Date/Time'])
    df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
    df['Proceeds'] = pd.to_numeric(df['Proceeds'], errors='coerce').astype(np.float64)
    df['Comm/Fee'] = pd.to_numeric(df['Comm/Fee'], errors='coerce').astype(np.float64)
    df['Basis'] = pd.to_numeric(df['Basis'], errors='coerce').astype(np.float64)
    df['Realized P/L'] = pd.to_numeric(df['Realized P/L'], errors='coerce').astype(np.float64)
    df['MTM P/L'] = pd.to_numeric(df['MTM P/L'], errors='coerce').astype(np.float64)
    df['T. Price'] = pd.to_numeric(df['T. Price'], errors='coerce').astype(np.float64)
    df['C. Price'] = pd.to_numeric(df['C. Price'], errors='coerce').astype(np.float64)
    if 'Display Suffix' not in df.columns:
        df['Display Suffix'] = ''
    df['Display Suffix'] = df['Display Suffix'].fillna('').astype(str)
    if 'Manual' not in df.columns:
        df['Manual'] = False
    if 'Action' not in df.columns and 'Code' in df.columns:
        def determine_action(code):
            actions = code.strip().split(';')
            if 'O' in actions or 'Ca' in actions:
                if 'C' in actions:
                    return 'Close/Open'
                return 'Open'
            elif 'C' in actions:
                return 'Close'
            else:
                return 'Unknown'
        df['Action'] = df['Code'].apply(determine_action)
    if 'Code' in df.columns:
        df['Code'] = df['Code'].fillna('')
    if 'Option Type' not in df.columns:
        df['Option Type'] = ''
    # If action is not Transfer, then Type is Long if we're opening a position, Short if closing
    def get_type(row):
        if row['Quantity'] == 0:
            return None
        if row['Action'] == 'Transfer':
            return 'In' if row['Quantity'] > 0 else 'Out'
        codes = row['Code'].strip().split(';') if 'Code' in row else []
        if 'Ex' in codes:
            return 'Exercised'
        if 'Ep' in codes:
            return 'Expired'
        if 'A' in codes:
            return 'Assigned'
        if (row['Action'] == 'Close' and row['Quantity'] < 0) or (row['Action'] == 'Open' and row['Quantity'] > 0):
            return 'Long'
        return 'Short'
    if 'Type' not in df.columns:
        df['Type'] = None
    df['Type'] = df['Type'].fillna(df.apply(get_type, axis=1))
    return df

def normalize_trades(df: pd.DataFrame) -> pd.DataFrame:
    """ Normalize the trade DataFrame by converting columns, then adding derived columns and hashing the result as its index (used to determine import uniqueness). """
    if not df.empty:
        df = convert_trade_columns(df)
        df['Year'] = df['Date/Time'].dt.year
        df['Orig. Quantity'] = df['Quantity']
        df['Orig. T. Price'] = df['T. Price']
        df['Category'] = 'Trades'
        df = df[['Category'] + [col for col in df.columns if col != 'Category']]
    # Set up the hash column as index
    df['Hash'] = df.apply(hash.hash_row, axis=1)
    df.set_index('Hash', inplace=True)
    # st.write('Imported', len(df), 'rows')
    return df

@st.cache_data()
def merge_trades(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """ Merge two already processed DataFrames of trades, removing duplicates. """
    if existing is None:
        return new
    if len(new) == 0:
        return existing
    merged = pd.concat([existing, new])
    return merged[~merged.index.duplicated(keep='first')]

def add_split_data(target: pd.DataFrame, split_actions: pd.DataFrame) -> pd.DataFrame:
    """ Compute cumulative split ratio column based on split actions."""
    target['Split Ratio'] = 1.0
    if split_actions is None or split_actions.empty:
        return target

    split_actions = split_actions[split_actions['Action'] == 'Split']
    if not split_actions.empty:
        # Sort split actions by date and compute cumulative ratio
        split_actions = split_actions.sort_values(by='Date/Time', ascending=False)
        split_actions['Cumulative Ratio'] = split_actions.groupby('Symbol')['Ratio'].cumprod()

        # Merge target with split_actions to align split ratios with trades. We'll have many-to-one mapping here, to be cleaned later.
        merged = target.reset_index().merge(split_actions, on='Symbol', suffixes=('', '_split'), how='left')
        index_col = 'Hash' if 'Hash' in merged.columns else 'index' 

        # Filter out rows where the split action date is in the future relative to the trade date
        filtered = merged[merged['Date/Time_split'] >= merged['Date/Time']]

        # Create a binding that can be used to look up the correct split ratio (one with the latest date) for each trade
        split_lookup = filtered.groupby(index_col)['Date/Time_split'].idxmin()
        
        # Apply the lookup to bind to the cumulative ratio
        target.loc[split_lookup.index, 'Split Ratio'] = 1 / filtered.loc[split_lookup, 'Cumulative Ratio'].values

        # Fill NaN values with 1 (for trades with no applicable split actions)
        target['Split Ratio'].fillna(1.0, inplace=True)

    return target

def compute_accumulated_positions(trades: pd.DataFrame) -> pd.DataFrame:
    """ Compute accumulated positions for each symbol by simulating all trades. Transfers are now excluded from the computation. """
    # trades = trades[trades['Action'] != 'Transfer']
    trades.sort_values(by=['Date/Time'], inplace=True)
    trades['Accumulated Quantity'] = trades.groupby(['Ticker', 'Display Suffix'])['Quantity'].cumsum().astype(np.float64)
    # Now also compute accumulated quantity per account
    trades['Account Accumulated Quantity'] = trades.groupby(['Account', 'Ticker', 'Display Suffix'])['Quantity'].cumsum().astype(np.float64)
    return _split_open_close_transactions(trades)

def _split_open_close_transactions(trades: pd.DataFrame) -> pd.DataFrame:
    """ Splits any transactions that are both opening and closing a position into two. This is needed for tax optimization as it's actually transitioning between short and long positions. """
    close_open = trades[(trades['Action'] == 'Close/Open') & (trades['Quantity'] != 0)]
    close_open = close_open[close_open['Accumulated Quantity'] < close_open['Quantity']] # Filter out transactions that are not transitioning between long and short
    if close_open.empty:
        return trades
    split_trades = pd.DataFrame()
    for index, row in close_open.iterrows():
        # Split each transaction into two at the point of zero accumulated quantity. The previous accumulated quantity should be negative.
        closing_row, opening_row = _split_transaction_at_quantity(row, row['Accumulated Quantity'])
        df = pd.DataFrame([closing_row, opening_row])
        split_trades = pd.concat([split_trades, df])
    trades = trades.drop(close_open.index)
    split_trades.set_index('Hash', inplace=True)
    trades = pd.concat([trades, split_trades])
    return trades

def _split_transaction_at_quantity(transaction: pd.Series, quantity: float) -> tuple[pd.Series, pd.Series]:
    """ Split a transaction into two at the given quantity. """
    assert (transaction['Quantity'] > 0 and 0 <= quantity <= transaction['Quantity']) or (transaction['Quantity'] < 0 and transaction['Quantity'] <= quantity <= 0, "Quantity needs to be within the transaction bounds.")
    prev_accumulated_quantity = transaction['Accumulated Quantity'] - transaction['Quantity']
    closing_row = transaction.copy()
    opening_row = transaction.copy()
    closing_row['Quantity'] = quantity
    opening_row['Quantity'] = opening_row['Quantity'] - quantity
    closing_row['Accumulated Quantity'] = prev_accumulated_quantity + quantity
    closing_row['Account Accumulated Quantity'] = transaction['Account Accumulated Quantity'] - transaction['Quantity'] + quantity
    closing_row['Action'] = 'Close' if closing_row['Quantity'] + prev_accumulated_quantity <= 0 else 'Open'
    opening_row['Action'] = 'Close' if transaction['Accumulated Quantity'] <= 0 else 'Open'
    closing_row['Type'] = 'Short' if (closing_row['Action'] == 'Close' and closing_row['Quantity'] > 0) or (closing_row['Action'] == 'Open' and closing_row['Quantity'] < 0) else 'Long'
    opening_row['Type'] = 'Short' if (opening_row['Action'] == 'Close' and opening_row['Quantity'] > 0) or (opening_row['Action'] == 'Open' and opening_row['Quantity'] < 0) else 'Long'
    fraction = quantity / transaction['Quantity']
    closing_row['Proceeds'] = fraction * transaction['Proceeds']
    opening_row['Proceeds'] = (1 - fraction) * transaction['Proceeds']
    closing_row['Comm/Fee'] = fraction * transaction['Comm/Fee']
    opening_row['Comm/Fee'] = (1 - fraction) * transaction['Comm/Fee']
    closing_row['Basis'] = fraction * transaction['Basis']
    opening_row['Basis'] = (1 - fraction) * transaction['Basis']
    closing_row['Realized P/L'] = fraction * transaction['Realized P/L']
    opening_row['Realized P/L'] = (1 - fraction) * transaction['Realized P/L']
    closing_row['MTM P/L'] = fraction * transaction['MTM P/L']
    opening_row['MTM P/L'] = (1 - fraction) * transaction['MTM P/L']
    closing_row['Hash'] = hash.hash_row(closing_row)
    opening_row['Hash'] = hash.hash_row(opening_row)
    return closing_row, opening_row

def positions_with_missing_transactions(trades: pd.DataFrame) -> pd.DataFrame:
    """ 
    Identify positions that should be closing positions entirely but our accumulated quantity is not zero. 
    This implies that we're missing some of the opening transactions.
    """
    # If it's a closing transaction, quantity and accumulated quantity should have opposite signs (closing the position should mean reducing it towards zero)
    return trades[(trades['Action'] == 'Close') & (trades['Accumulated Quantity'] != 0) & ((trades['Accumulated Quantity'] * trades['Quantity']) >= 0)]

def per_account_transfers_with_missing_transactions(trades: pd.DataFrame) -> pd.DataFrame:
    """ Identify transfers that do not have corresponding accumulated quantity in the source account """
    return trades[(trades['Action'] == 'Transfer') & (trades['Type'] == 'Out') & (trades['Quantity'] < 0) & (trades['Account Accumulated Quantity'] < 0)]


def transfers_with_missing_transactions(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """  
    Identify transfers that do not have a corresponding outgoing transfer in the account they are transferring from. 
    This implies that the source account history was not completely imported and we don't know all the purchase prices.
    Returns: unmatched incoming, unmatched outgoing
    """
    transfers = trades[(trades['Action'] == 'Transfer') & (trades['Type'] != 'Spinoff')] 
    outgoing = transfers[transfers['Type'] == 'Out']
    if 'Target' in trades.columns:
        incoming = transfers[transfers['Type'] == 'In']
        # Compute over outgoing account name
        incoming_grouped = incoming.groupby(['Display Name', 'Account'])['Quantity'].sum()
        outgoing_grouped = outgoing.groupby(['Display Name', 'Target'])['Quantity'].sum()
        outgoing_grouped.index = outgoing_grouped.index.set_names('Account', level=1)
        unmatched_outgoing = outgoing_grouped.add(incoming_grouped, fill_value=0)
        unmatched_outgoing = unmatched_outgoing[unmatched_outgoing < 0]
        # Do it again to persist incoming account names
        incoming_grouped = incoming.groupby(['Display Name', 'Target'])['Quantity'].sum()
        outgoing_grouped = outgoing.groupby(['Display Name', 'Account'])['Quantity'].sum()
        outgoing_grouped.index = outgoing_grouped.index.set_names('Target', level=1)
        unmatched_incoming = incoming_grouped.add(outgoing_grouped, fill_value=0)
        unmatched_incoming = unmatched_incoming[unmatched_incoming > 0]
        return unmatched_incoming, unmatched_outgoing
    
    return pd.DataFrame(), pd.DataFrame()

# Adjust quantities and trade prices for splits
def adjust_for_splits(trades: pd.DataFrame, split_actions: pd.DataFrame) -> pd.DataFrame:
    """ Adjust trade quantities and prices for already detected stock splits. """
    if 'Split Ratio' not in trades.columns:
        trades['Split Ratio'] = np.nan
    if split_actions is not None and not split_actions.empty:
        add_split_data(trades, split_actions)
        trades['Quantity'] = trades['Orig. Quantity'] * trades['Split Ratio']
        trades['T. Price'] = trades['Orig. T. Price'] / trades['Split Ratio']
    return trades
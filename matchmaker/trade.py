import numpy as np
import pandas as pd
import streamlit as st
from matchmaker import hash

# Ensure all columns are in non-string format
def convert_trade_columns(df):
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
    if 'Action' not in df.columns and 'Code' in df.columns:
       df['Action'] = df['Code'].apply(lambda x: 'Open' if ('O' in x or 'Ca' in x) else 'Close' if 'C' in x else 'Unknown')
    # If action is not Transfer, then Type is Long if we're opening a position, Short if closing
    def get_type(row):
        if row['Quantity'] == 0:
            return None
        if row['Action'] == 'Transfer':
            return 'In' if row['Quantity'] > 0 else 'Out'
        if (row['Action'] == 'Close' and row['Quantity'] < 0) or (row['Action'] == 'Open' and row['Quantity'] > 0):
            return 'Long'
        return 'Short'
    if 'Type' not in df.columns:
        df['Type'] = None
    df['Type'] = df['Type'].fillna(df.apply(get_type, axis=1))
    return df

# Process trades from raw DataFrame
def normalize_trades(df):
    if df.empty:
        return df
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

# Merge two sets of processed trades together
@st.cache_data()
def merge_trades(existing, new):
    if existing is None:
        return new
    if len(new) == 0:
        return existing
    merged = pd.concat([existing, new])
    return merged[~merged.index.duplicated(keep='first')]

# Add split data column to trades by consulting split actions
def add_split_data(target, split_actions):
    target['Split Ratio'] = 1.0
    if split_actions is None or split_actions.empty:
        return target
    split_actions = split_actions[split_actions['Action'] == 'Split']
    if not split_actions.empty:
        # Enhance trades with Split Ratio column by looking up same symbol in split_actions
        #  and summing all ratio columns that have a date sooner than the row in trades    
        split_actions = split_actions.sort_values(by='Date/Time', ascending=True)
        split_actions['Cumulative Ratio'] = split_actions.groupby('Symbol')['Ratio'].cumprod()
        target['Split Ratio'] = 1 / target.apply(lambda row: split_actions[(split_actions['Symbol'] == row['Symbol']) & (split_actions['Date/Time'] > row['Date/Time'])]['Cumulative Ratio'].min(), axis=1)
        target['Split Ratio'].fillna(1.0, inplace=True)
        split_actions.drop(columns=['Cumulative Ratio'], inplace=True)
    return target

# Compute accumulated positions for each symbol by simulating all trades
@st.cache_data()
def compute_accumulated_positions(trades, symbols):
    trades.sort_values(by=['Date/Time'], inplace=True)
    trades['Accumulated Quantity'] = trades.groupby(['Ticker', 'Display Suffix'])['Quantity'].cumsum().astype(np.float64)
    # Now also compute accumulated quantity per account'
    trades['Account Accumulated Quantity'] = trades.groupby(['Account', 'Ticker', 'Display Suffix'])['Quantity'].cumsum().astype(np.float64)
    return trades

def per_account_transfers_with_missing_transactions(trades):
    return trades[(trades['Action'] == 'Transfer') & (trades['Type'] == 'Out') & (trades['Quantity'] < 0) & (trades['Account Accumulated Quantity'] < 0)]

def positions_with_missing_transactions(trades):
    return trades[((trades['Accumulated Quantity'] < 0) & (trades['Type'] == 'Long') & (trades['Action'] == 'Close') | 
                  (trades['Accumulated Quantity'] > 0) & (trades['Type'] == 'Short') & (trades['Action'] == 'Close'))]
    
def transfers_with_missing_transactions(trades):
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
    
    return pd.DataFrame()

# Adjust quantities and trade prices for splits
@st.cache_data()
def adjust_for_splits(trades, split_actions):
    if 'Split Ratio' not in trades.columns:
        trades['Split Ratio'] = np.nan
    if split_actions is not None and not split_actions.empty:
        add_split_data(trades, split_actions)
        trades['Quantity'] = trades['Orig. Quantity'] * trades['Split Ratio']
        trades['T. Price'] = trades['Orig. T. Price'] / trades['Split Ratio']
    return trades
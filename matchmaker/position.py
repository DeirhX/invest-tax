import pandas as pd
import numpy as np
import streamlit as st
from typing import Optional, Tuple

def convert_position_history_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert columns of the position history DataFrame to appropriate types.
    """
    df['Prior Date'] = pd.to_datetime(df['Prior Date'])
    df['Date'] = pd.to_datetime(df['Current Date'])
    df['Prior Quantity'] = pd.to_numeric(df['Prior Quantity'], errors='coerce')
    df['Quantity'] = pd.to_numeric(df['Current Quantity'], errors='coerce')
    df['Prior Price'] = pd.to_numeric(df['Prior Price'], errors='coerce')
    df['Price'] = pd.to_numeric(df['Current Price'], errors='coerce')
    df['Mark-to-Market P/L Position'] = pd.to_numeric(df['Mark-to-Market P/L Position'], errors='coerce')
    df['Mark-to-Market P/L Transaction'] = pd.to_numeric(df['Mark-to-Market P/L Transaction'], errors='coerce')
    df['Mark-to-Market P/L Commissions'] = pd.to_numeric(df['Mark-to-Market P/L Commissions'], errors='coerce')
    df['Mark-to-Market P/L Other'] = pd.to_numeric(df['Mark-to-Market P/L Other'], errors='coerce')
    df['Mark-to-Market P/L Total'] = pd.to_numeric(df['Mark-to-Market P/L Total'], errors='coerce')
    df['Category'] = 'Open Positions'
    df = df[['Category'] + [col for col in df.columns if col != 'Category']]
    return df

def compute_open_positions(trades: pd.DataFrame, time: pd.Timestamp = pd.Timestamp.now()) -> pd.DataFrame:
    """
    Compute open positions per symbol at a given time.
    """
    trades = trades[trades['Date/Time'] <= time]
    positions = trades.groupby('Ticker')[['Accumulated Quantity', 'Date/Time', 'Split Ratio']].last().reset_index()
    return positions[np.abs(positions['Accumulated Quantity']) > 1e-9]

def compute_open_positions_per_account(trades: pd.DataFrame, time: pd.Timestamp = pd.Timestamp.now(), account: Optional[str] = None) -> pd.DataFrame:
    """
    Compute open positions per symbol at a given time for a specific account.
    """
    trades = trades[trades['Date/Time'] <= time]
    if account is not None:
        trades = trades[trades['Account'] == account]
    positions = trades.groupby('Display Name')[['Account', 'Account Accumulated Quantity', 'Date/Time', 'Split Ratio']].last().reset_index()

    return positions[np.abs(positions['Account Accumulated Quantity']) > 1e-9]

def check_open_position_mismatches(trades: pd.DataFrame, positions: pd.DataFrame, symbols: pd.DataFrame, max_date: pd.Timestamp = pd.Timestamp.now()) -> pd.DataFrame:
    """
    Check for mismatches between computed open positions and position snapshots. Tries to guess symbol renames as those are not in the IBKR history.
    Returns:
        Tuple[pd.DataFrame, pd.DataFrame]: Misaligned positions, guessed renames
    """
    time_points = positions[(positions['Quantity'] != 0) & (positions['Date'] <= max_date)].groupby(['Date', 'Account'])
    mismatches = pd.DataFrame(columns=['Ticker', 'Display Name', 'Currency', 'Date'])
    for (time, account), snapshot in time_points:
        # Join and check for quantity mismatches or missing symbols
        time = pd.Timestamp(time).replace(hour=23, minute=59, second=59) # Position snapshots are taken at the end of the day
        open_positions = compute_open_positions_per_account(trades, time, account).drop(columns=['Split Ratio'])
        merged = snapshot.merge(open_positions, on=['Display Name', 'Account'], suffixes=(' Positions', ' Trades'), how='outer')
        merged['Quantity Mismatch'] = merged['Account Accumulated Quantity'].fillna(0) - merged['Quantity'].fillna(0) * merged['Split Ratio'].fillna(1)
        merged['Snapshot Date'] = time
        new_mismatches = merged[np.abs(merged['Quantity Mismatch']) > 1e-9]
        mismatches = pd.concat([mismatches, new_mismatches])

    if mismatches.empty:
        return mismatches

    mismatches.drop_duplicates(subset=['Ticker', 'Display Name', 'Symbol', 'Date'], inplace=True)
    mismatches.reset_index(drop=True, inplace=True)
    mismatches['Date'] = mismatches['Date/Time Positions'].fillna(mismatches['Date/Time Trades'])
    # Compute the date range for each symbol activity so we make filter out overlapping symbols from the guesses
    agg_funcs = {
        'Date/Time': ['min', 'max']
    }
    symbol_dates = trades.groupby('Ticker').agg(agg_funcs).reset_index()
    symbol_dates.columns = ['Display Name', 'First Activity', 'Last Activity']
    # Group by possibly renamed symbols and check if we have pairs of mismatches
    mismatches = mismatches.merge(symbol_dates, on='Display Name', how='left')
    return mismatches

def detect_renames_in_mismatches(mismatches: pd.DataFrame, symbols: pd.DataFrame) -> pd.DataFrame:
    if mismatches.empty:
        return pd.DataFrame()
    guesses = pd.DataFrame(columns=['From', 'To', 'Action', 'Change Date'])
    mismatches = mismatches[mismatches['Display Name'].isin(symbols.index)]
    
    grouped_mismatches = mismatches.sort_values(by='Last Activity').groupby([mismatches['Quantity Mismatch'].abs(), mismatches['Snapshot Date']])
    for name, group in grouped_mismatches:
        if len(group) == 2:
            # Don't consider the symbol to be renamed if both symbols have overlapping activity in the trade history
            from_row = group.iloc[0]
            to_row = group.iloc[1]
            if (not pd.isna(to_row['First Activity']) and not pd.isna(from_row['First Activity']) and from_row['Last Activity'] > to_row['First Activity']):
                continue
            if (symbols[symbols.index == from_row['Display Name']]['Currency'].values[0] != symbols[symbols.index == to_row['Display Name']]['Currency'].values[0]):
                continue
            action = 'Rename'
            row = pd.DataFrame([{'From': from_row['Display Name'], 'To': to_row['Display Name'], 'Action': action, 'Change Date': from_row['Snapshot Date'], 'Year': int(from_row['Snapshot Date'].year)}])
            guesses = pd.concat([guesses, row])

    if not guesses.empty:
        # Return only mismatches with no entry in guesses (From and To)
        mismatches = mismatches[~mismatches['Display Name'].isin(guesses['From'])]
        mismatches = mismatches[~mismatches['Display Name'].isin(guesses['To'])]
    guesses.rename(columns={'From': 'Symbol', 'To': 'Ticker'}, inplace=True)
    return guesses
import pandas as pd
import numpy as np
import glob
import argparse
import os
import json
import sys
import streamlit as st
from matchmaker.trades import *
from matchmaker.ibkr import *
from matchmaker.pairing import *
from matchmaker.currency import *
import matchmaker.data as data

streamlit = True



# Load Trades CSV as DataFrame
def import_trades(directory, tickers_dir=None):
    merged = None
    for trades in import_all_statements(directory, tickers_dir):
        merged = merge_trades(merged, trades)
    populate_extra_trade_columns(merged, tickers_dir)
    return merged    

def import_trade_file(file):
    # Read first line to determine the format
    header = file.readline()
    header = header.decode('utf-8').strip()
    file.seek(0)
    try:
        if header.startswith('Hash'):
            return import_raw_trades(file)
        else:
            return import_activity_statement(file)
    except Exception as e:
        st.error(f'Error importing trades. File {file.name} does not contain the expected format. Error: {e}')
        return pd.DataFrame()
    

def main():
    st.set_page_config(page_title='Krutopřísný tradematcher', layout='centered')
    data.load_settings()
    # st.header('Taxonomy Matchmaker')
    st.subheader('Import Trades From IBKR Activity Statements')

    
    # Process command-line arguments
    parser = argparse.ArgumentParser(description='Process command-line arguments')

    # Add the arguments
    parser.add_argument('--settings-dir', type=str, help='Path to CurrencyRates.csv file')
    parser.add_argument('--import-trades-dir', type=str, help='Path to Trades CSV files')
    parser.add_argument('--tickers-dir', type=str, help='Path to load historic ticker data to adjust prices for splits')
    parser.add_argument('--load-trades', type=str, help='Path to load processed trades file')
    parser.add_argument('--save-trades', type=str, help='Path to save processed trades file after import')
    parser.add_argument('--process-years', type=str, help='List of years to process, separated by commas. If not specified, all years are processed.')
    parser.add_argument('--preserve-years', type=str, help='List of years to keep unchanged, separated by commas. If not specified, all years are preserved.')
    parser.add_argument('--strategy', type=str, default='fifo', help='Strategy to use for pairing buy and sell orders. Available: fifo, lifo, average-cost, max-loss. max-profit')
    parser.add_argument('--save-trade-overview-dir', type=str, help='Directory to output overviews of matched trades')
    parser.add_argument('--load-matched-trades', type=str, help='Paired trades input to load')
    parser.add_argument('--save-matched-trades', type=str, help='Save updated paired trades')
    
    # Parse the arguments
    args = parser.parse_args()

    trades = st.session_state.trades if 'trades' in st.session_state else pd.DataFrame()
    sell_buy_pairs = None
    process_years = None
    preserve_years = None

    # Load list of filenames from session state
    previous_uploads = st.session_state.filenames if 'filenames' in st.session_state else []

    # Show file upload widget
    uploaded_files = st.file_uploader("Choose a file", accept_multiple_files=True, type=['csv'])
    # Compare filenames to uploaded files.
    if [f.name for f in uploaded_files] != previous_uploads:
        # If less files were uploaded, we need to clear the trades. Until each row knows it source, reset it all
        if len(uploaded_files) < len(previous_uploads):
            trades = pd.DataFrame()
    # Save newly uploaded files to session
    st.session_state.filenames = [f.name for f in uploaded_files]
        
    import_state = st.caption('')
    trades_count = len(trades)
    loaded_count = 0
    # On upload, run import trades
    if uploaded_files:
        for uploaded_file in uploaded_files:
            import_state.write('Importing trades...')
            imported = import_trade_file(uploaded_file)
            loaded_count += len(imported)
            import_state.write(f'Merging :blue[{len(imported)}] trades...')
            trades = merge_trades(trades, imported)
            import_message = f'Imported :green[{len(trades) - trades_count}] trades.'
            populate_extra_trade_columns(trades, st.session_state['settings']['tickers_dir'])
            st.session_state.trades = trades
            import_state.write(import_message)
        import_state.write(f'Total trades loaded: :blue[{loaded_count}] of which :green[{len(trades) - trades_count}] were new.')
    # Show the parsed trades
    st.session_state.trades = trades
    st.caption(f'Trades found: :blue[{len(trades)}]')
    st.dataframe(data=trades, hide_index=True, width=1100, height=500, column_order=('Symbol', 'Date/Time', 'Quantity', 'Currency', 'T. Price', 'Proceeds', 'Comm/Fee', 'Realized P/L', 'Accumulated Quantity'),
                    column_config={
                        'Realized P/L': st.column_config.NumberColumn("Profit", format="%.1f"), 
                        'Proceeds': st.column_config.NumberColumn("Proceeds", format="%.1f"), 
                        'Comm/Fee': st.column_config.NumberColumn("Fees", format="%.1f"), 
                        'Quantity': st.column_config.NumberColumn("Quantity", format="%f"), 
                        'Accumulated Quantity': st.column_config.NumberColumn("Position", format="%f")})

    # Serve merged trades as CSV    
    @st.cache_data()
    def trades_to_csv(trades):
        return trades.to_csv().encode('utf-8')    
    if (len(trades) > 0):
        trades_csv = trades_to_csv(trades)
        st.download_button('Download trades in single file', trades_csv, 'merged_trades.csv', 'text/csv')
    return

    # Load data
    daily_rates = load_daily_rates(args.settings_dir)
    yearly_rates = load_yearly_rates(args.settings_dir)

    if args.load_trades is not None:
        trades = pd.read_csv(args.load_trades)
        trades = convert_trade_columns(trades)
        trades.set_index('Hash', inplace=True)
    if args.import_trades_dir is not None:
        trades = import_trades(args.import_trades_dir, trades, args.tickers_dir)
    if args.load_matched_trades is not None:
        sell_buy_pairs = load_buy_sell_pairs(args.load_matched_trades)
    if args.process_years is not None:
        process_years = [int(x) for x in args.process_years.split(',')]
    if args.preserve_years is not None:
        preserve_years = [int(x) for x in args.preserve_years.split(',')]

    # Pair buy and sell orders
    buys, sells, sell_buy_pairs = pair_buy_sell(trades, sell_buy_pairs, args.strategy, process_years, preserve_years)
    paired_sells = sells[sells['Uncovered Quantity'] == 0]
    unpaired_sells = sells[sells['Uncovered Quantity'] != 0]
    paired_buys = buys[buys['Uncovered Quantity'] == 0]
    unpaired_buys = buys[buys['Uncovered Quantity'] != 0]

    # Save unpaired sells to CSV
    sort_columns = ['Symbol', 'Date/Time']
    if args.save_trades:
        trades.drop(['Covered Quantity', 'Uncovered Quantity'], axis=1, inplace=False).sort_values(by=sort_columns).round(3).to_csv(args.save_trades, index=True)
    if args.save_trade_overview_dir:
        sells.round(3).sort_values(by=sort_columns).to_csv(args.save_trade_overview_dir + '/sells.csv', index=False)
        paired_sells.round(3).sort_values(by=sort_columns).to_csv(args.save_trade_overview_dir + '/sells.paired.csv', index=False)
        unpaired_sells.round(3).sort_values(by=sort_columns).to_csv(args.save_trade_overview_dir + '/sells.unpaired.csv', index=False)
        buys.round(3).sort_values(by=sort_columns).to_csv(args.save_trade_overview_dir + '/buys.csv', index=False)
        paired_buys.round(3).sort_values(by=sort_columns).to_csv(args.save_trade_overview_dir + '/buys.paired.csv', index=False)
        unpaired_buys.round(3).sort_values(by=sort_columns).to_csv(args.save_trade_overview_dir + '/buys.unpaired.csv', index=False)
    if args.save_matched_trades:
        sell_buy_pairs.round(3).to_csv(args.save_matched_trades, index=False)
        yearly_pairs = add_czk_conversion(sell_buy_pairs, yearly_rates, True)
        daily_pairs = add_czk_conversion(sell_buy_pairs, daily_rates, False)
        for year in sorted(trades['Year'].unique()):
            for pairs in [yearly_pairs, daily_pairs]:
                filtered_pairs = pairs[(pairs['Sell Time'].dt.year == year)].sort_values(by=['Symbol','Sell Time', 'Buy Time'])
                taxed_pairs = filtered_pairs[filtered_pairs['Taxable'] == 1]
                pairing_type = 'yearly' if pairs is yearly_pairs else 'daily'
                print(f'Pairing for year {year} using {pairing_type} rates in CZK: Proceeds {taxed_pairs["CZK Proceeds"].sum().round(0)}, '
                    f'Cost {taxed_pairs["CZK Cost"].sum().round(0)}, Revenue {taxed_pairs["CZK Revenue"].sum().round(0)}, '
                    f'Untaxed pairs: {len(filtered_pairs) - len(taxed_pairs)}')
                filtered_pairs[filtered_pairs['Sell Time'].dt.year == year].round(3).to_csv(args.save_matched_trades + ".{0}.{1}.csv".format(year, pairing_type), index=False)
            unpaired_sells[unpaired_sells['Year'] == year].round(3).to_csv(args.save_matched_trades + ".{0}.unpaired.csv".format(year), index=False)

if __name__ == "__main__":
    main()

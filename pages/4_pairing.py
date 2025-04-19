import pandas as pd
import streamlit as st
from streamlit_pills import pills
import matchmaker.currency as currency
import matchmaker.data as data 
import matchmaker.ux as ux
import matchmaker.trade as trade
import matchmaker.styling as styling
import matchmaker.pairing as pairing
from menu import menu
import copy

def page():
    """ Streamlit page for tax overview and pairing trades. """
    st.set_page_config(page_title='Daňový přehled', layout='wide')
    menu()
    data.load_settings()

    state = data.State()
    state.load_session()
    if state.trades.empty:
        st.caption('Nebyly importovány žádné obchody.')
        st.page_link("pages/1_import_trades.py", label="📥 Přejít na import obchodů")
        return
        
    st.caption(str(len(state.trades)) + ' obchodů k dispozici.')
    # Matching configuration is a dictionary[year] of:
    #  strategy: FIFO, LIFO, AverageCost, MaxLoss, MaxProfit
    #  use_yearly_rates: bool
    trades = state.trades[(state.trades['Action'] == 'Open') | (state.trades['Action'] == 'Close')] # Filter out transfers and other transactions
    closing_trades = trades[trades['Action'] == 'Close']
    strategies = pairing.Pairings.get_strategies()[1:]
    
    if closing_trades.empty:
        st.caption('Nebyly nalezeny žádné uzavřené obchody. Pro párování je třeba mít alespoň jeden uzavřený obchod.')
        return
    
    st.session_state.update(year=ux.add_years_filter(closing_trades, False, 'Rok pro párování'))
    years = sorted(closing_trades['Year'].unique())
    show_year = st.session_state.get('year')
    if show_year is None:
        show_year = years[-1]
    state.pairings.populate_choices(trades)
    
    choice = copy.deepcopy(state.pairings.config[show_year])
    if (choice.pair_strategy == 'None'):
        choice.pair_strategy = 'FIFO'
    choice.pair_strategy = pills('Strategie párování', strategies, index=strategies.index(choice.pair_strategy), key=f'strategy_{show_year}')
    choice.conversion_rates = 'Yearly' if pills(f'Použíté kurzy', ['roční', 'denní'], index=0 if choice.conversion_rates == 'Yearly' else 1, key=f'yearly_rates_{show_year}') == 'roční' else 'Daily'
    
    st.caption(f'Strategie pro rok {show_year}: {choice.pair_strategy} | {"roční" if choice.conversion_rates == 'Yearly' else "denní"} kurzy')
    
    state.pairings.populate_pairings(trades, show_year, choice)
    state.save_session()

    st.session_state.update(show_year=show_year)

    if state.pairings.paired.empty:
        st.caption('Nebyly nalezeny žádné párované obchody.')
        return
    
    filtered_pairs = state.pairings.paired[state.pairings.paired['Sell Time'].dt.year == show_year]
    footer = f'Danitelný výdělek: :blue[{filtered_pairs[filtered_pairs["Taxable"] == 1]["CZK Revenue"].sum():,.0f}] Kč'
    untaxed_revenue = filtered_pairs[filtered_pairs['Taxable'] == 0]['CZK Revenue'].sum()
    if untaxed_revenue > 0:
        footer += f' (z toho :grey[{untaxed_revenue:,.0f}] Kč osvobozeno od daně)'
    st.caption(footer)

    trades_display = st.dataframe(styling.format_paired_trades(filtered_pairs), hide_index=True, height=600, 
                                column_order=('Display Name','Quantity','Buy Time','Buy Price','Sell Time','Sell Price','Currency','Buy Cost','Sell Proceeds','Revenue',
                                            'CZK Revenue','Percent Return','Type','Taxable','Buy CZK Rate','Sell CZK Rate', 'CZK Cost','CZK Proceeds'),
                                column_config={
                                    'Display Name': st.column_config.TextColumn("Název", help="Název instrumentu"),
                                    'Quantity': st.column_config.NumberColumn("Počet", help="Počet kusů daného instrumentu", format="%d" if choice.pair_strategy != 'AverageCost' else "%.2f"), 
                                    'Buy Time': st.column_config.DatetimeColumn("Datum nákupu", help="Datum nákupní transakce"), 
                                    'Sell Time': st.column_config.DatetimeColumn("Datum prodeje", help="Datum prodejní transakce"), 
                                    'Buy Price': st.column_config.NumberColumn("Nákup (hrubý)", help="Cena nákupu 1 kusu bez poplatků", format="%.2f"), 
                                    'Sell Price': st.column_config.NumberColumn("Prodej (hrubý)", help="Cena prodeje 1 kusu bez poplatků", format="%.2f"), 
                                    'Buy Cost': st.column_config.NumberColumn("Nákup (čistý)", help="Cena nákupu 1 kusu včetně poplatků", format="%.2f"), 
                                    'Sell Proceeds': st.column_config.NumberColumn("Prodej (čistý)", help="Cena prodeje 1 kusu včetně poplatků", format="%.2f"), 
                                    'Revenue': st.column_config.NumberColumn("Výdělek (čistý)", help="Zisk z prodeje mínus cena nákupu včetně poplatků", format="%.1f"), 
                                    'Currency': st.column_config.TextColumn("Měna", help="Měna v které bylo obchodováno"), 
                                    'Percent Return': st.column_config.NumberColumn("Návratnost", help="Návratnost obchodu po odečtení všech nákladů včetně poplatků", format="%.0f%%"), 
                                    'Taxable': st.column_config.CheckboxColumn("Daní se", help="Prodej se daní, pokud nebyl spárován s nákupem starším 3 let (časový test)"),
                                    'Type': st.column_config.TextColumn("Typ", help="Long nebo short pozice. Long pozice je standardní nákup instrumentu pro pozdější prodej s očekáváním zvýšení ceny. Short pozice je prodej instrumentu, který ještě nevlastníte, s očekáváním poklesu ceny a následného nákupu."),
                                    'Buy CZK Rate': st.column_config.NumberColumn("Nákupní kurz", help="Kurz měny v době nákupu", format="%.2f"),
                                    'Sell CZK Rate': st.column_config.NumberColumn("Prodejní kurz", help="Kurz měny v době prodeje", format="%.2f"),
                                    'CZK Cost': st.column_config.NumberColumn("Náklady v CZK", format="%.1f"), 
                                    'CZK Revenue': st.column_config.NumberColumn("Výdělek v CZK", format="%.1f"), 
                                    'CZK Proceeds': st.column_config.NumberColumn("Příjem v CZK", format="%.1f"), 
                                    'Accumulated Quantity': st.column_config.NumberColumn("Position")
                                    })
    
    dividends = state.dividends[state.dividends['Date'].dt.year == show_year]
    if not dividends.empty:
        # Group dividends by currency and sum the amounts
        sums = dividends.groupby(['Currency']).agg({'Amount': 'sum'}).reset_index()
        # Convert into a line of text
        dividend_summary = ', '.join([f'{row["Amount"]:.0f} {row["Currency"]}' for _, row in sums.iterrows()])
        with st.expander(f'Celkový zisk z dividend: :green[{dividend_summary}]  ({dividends["Tax Percent"].abs().mean():.0f}% průměrná daň)]'):
            table_descriptor = ux.dividends_table_descriptor()
            st.dataframe(dividends, hide_index=True, column_config=table_descriptor['column_config'], column_order=table_descriptor['column_order'])
    
   
    unpaired_sells = state.pairings.unpaired[state.pairings.unpaired['Action'] == 'Close']
    unpaired_sells = unpaired_sells[(unpaired_sells['Year'] == show_year)]
    if not unpaired_sells.empty:
        st.caption(f'Pozor, jsou zde nenapárované prodeje: :red[{len(unpaired_sells)}]')
        st.subheader('Nenapárované obchody')
        table_descriptor = ux.transaction_table_descriptor_czk()
        st.dataframe(styling.format_trades(unpaired_sells), hide_index=True, column_config=table_descriptor['column_config'], column_order=table_descriptor['column_order'])
        ux.add_trades_editor(state, unpaired_sells.iloc[0])

page()
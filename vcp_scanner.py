"""
INTEGRATED VCP + SMART MONEY SCANNER
Enhanced version with Telegram notification formatting

Add this to the END of your existing vcp_scanner.py main() function
"""

# ============================================================================
# INTEGRATION CODE - Add this after your existing GitHub push code
# ============================================================================

def run_smart_money_analysis_and_push(filepath, sectors_filepath, date_str, gh_session, headers):
    """
    Run Smart Money Scanner and push results to GitHub
    
    Returns: (results_df, summary_text) for Telegram
    """
    print("\n" + "="*55)
    print("Running Smart Money Accumulation Analysis...")
    print("="*55)
    
    # Import the scanner
    import sys
    scanner_path = r'C:\PORTFOLIOS\FINANCE\Chartink\Smart Money Scanner'
    if scanner_path not in sys.path:
        sys.path.insert(0, scanner_path)
    
    try:
        from smart_money_scanner import SmartMoneyScanner
        
        # Run analysis
        scanner = SmartMoneyScanner(filepath, sectors_filepath)
        results_df = scanner.run()
        
        if results_df is not None and not results_df.empty:
            # Push full CSV to GitHub
            analysis_filename = f"{date_str}_Accumulation_Analysis.csv"
            ok = push_file_to_github(
                results_df.to_csv(index=False).encode('utf-8'),
                analysis_filename,
                gh_session,
                headers
            )
            if ok:
                print(f"  ✓ {analysis_filename}")
            
            # Generate Telegram summary
            telegram_summary = generate_telegram_summary(results_df, date_str)
            
            # Push summary as text file
            summary_filename = f"{date_str}_Telegram_Summary.txt"
            ok = push_file_to_github(
                telegram_summary.encode('utf-8'),
                summary_filename,
                gh_session,
                headers
            )
            if ok:
                print(f"  ✓ {summary_filename}")
            
            return results_df, telegram_summary
        
        else:
            print("  ⚠ No analysis results generated")
            return None, None
            
    except Exception as e:
        print(f"  ⚠ Smart Money analysis failed: {e}")
        print(f"  Error details: {str(e)[:100]}")
        return None, None


def generate_telegram_summary(results_df, date_str):
    """
    Generate Telegram-formatted summary
    
    Format:
    1. Summary table (all stocks with score >= 50)
    2. Detailed breakdown (top 3 stocks)
    """
    from datetime import datetime
    
    # Filter stocks with minimum recommendation (score >= 50)
    min_score = 50
    filtered_df = results_df[results_df['total_score'] >= min_score].copy()
    
    if filtered_df.empty:
        return f"📊 Smart Money Analysis - {date_str}\n\nNo stocks met minimum criteria (Score >= {min_score})"
    
    summary = []
    
    # Header
    summary.append("🎯 SMART MONEY ACCUMULATION REPORT")
    summary.append(f"📅 Date: {date_str}")
    summary.append(f"⏰ Generated: {datetime.now().strftime('%H:%M:%S')}")
    summary.append("")
    
    # Summary Statistics
    total_analyzed = len(results_df)
    qualified = len(filtered_df)
    block_buying_count = filtered_df['block_buying_detected'].sum()
    
    summary.append(f"📈 Stocks Analyzed: {total_analyzed}")
    summary.append(f"✅ Qualified: {qualified} (Score >= {min_score})")
    summary.append(f"🚨 Block Buying Detected: {block_buying_count}")
    summary.append("")
    summary.append("─" * 50)
    summary.append("")
    
    # Summary Table (ALL qualified stocks)
    summary.append(f"📊 ALL QUALIFIED STOCKS (Score >= {min_score}):")
    summary.append("")
    
    # Table header
    summary.append("```")
    summary.append(f"{'#':<3} {'Symbol':<12} {'Score':<6} {'Block':<6} {'Signal':<12}")
    summary.append("─" * 45)
    
    # Table rows
    for i, row in enumerate(filtered_df.itertuples(), 1):
        block = "🚨YES" if row.block_buying_detected else "No"
        signal_short = row.signal.replace("🔥 ", "").replace("✅ ", "").replace("👀 ", "").replace("⚠️ ", "")
        
        summary.append(
            f"{i:<3} "
            f"{row.symbol:<12} "
            f"{row.total_score:<6.0f} "
            f"{block:<6} "
            f"{signal_short:<12}"
        )
    
    summary.append("```")
    summary.append("")
    
    # Key Metrics Table
    summary.append("📋 KEY METRICS:")
    summary.append("```")
    summary.append(f"{'Symbol':<12} {'Del%':<6} {'VolDist':<8} {'FII%':<6} {'Bonus':<6}")
    summary.append("─" * 45)
    
    for row in filtered_df.head(10).itertuples():
        summary.append(
            f"{row.symbol:<12} "
            f"{row.delivery_pct:<6.1f} "
            f"{row.volume_distribution:<8.1f} "
            f"{row.fii_holding:<6.1f} "
            f"{row.multibagger_bonus:<6.0f}"
        )
    
    summary.append("```")
    summary.append("")
    summary.append("─" * 50)
    summary.append("")
    
    # Detailed Breakdown (Top 3)
    summary.append("🔍 DETAILED ANALYSIS (Top 3):")
    summary.append("")
    
    for i, row in enumerate(filtered_df.head(3).itertuples(), 1):
        summary.append(f"#{i}. {row.symbol} — {row.total_score:.0f}/100 — {row.signal}")
        summary.append("")
        summary.append("  📊 Score Breakdown:")
        summary.append(f"     • Volume Conviction:    {row.volume_conviction:.0f}/40")
        summary.append(f"     • Accumulation Pattern: {row.accumulation_pattern:.0f}/30")
        summary.append(f"     • Sector Strength:      {row.sector_strength:.0f}/20")
        summary.append(f"     • Institutional Score:  {row.institutional_score:.0f}/10")
        summary.append(f"     • 🆕 Multibagger Bonus:  {row.multibagger_bonus:.0f}/20")
        summary.append("")
        summary.append("  🎯 Multibagger Signals:")
        summary.append(f"     🚨 Block Buying (30-40x): {'YES ✓' if row.block_buying_detected else 'No'}")
        summary.append(f"     📊 Sustained Volume:      {'YES ✓' if row.sustained_volume else 'No'}")
        summary.append(f"     🔄 Price Tightening:      {'YES ✓' if row.price_tightening else 'No'}")
        summary.append(f"     📈 Volume Distribution:   {row.volume_distribution:.1f}%")
        summary.append("")
        summary.append("  📈 Other Metrics:")
        summary.append(f"     • Delivery %:        {row.delivery_pct:.1f}%")
        summary.append(f"     • FII Holdings:      {row.fii_holding:.1f}%")
        summary.append(f"     • DII Holdings:      {row.dii_holding:.1f}%")
        summary.append(f"     • Volume Trend:      {row.volume_trend}")
        summary.append(f"     • 5-Day Momentum:    {row.price_momentum:+.2f}%")
        summary.append("")
        summary.append("─" * 50)
        summary.append("")
    
    # Footer notes
    if block_buying_count > 0:
        summary.append("🚨 ALERT: Block Buying detected!")
        summary.append("   Institutional entry (30-40x volume spike)")
        summary.append("   Priority stocks for immediate review!")
        summary.append("")
    
    summary.append("Legend:")
    summary.append("  Block: 🚨YES = Institutional block buying detected")
    summary.append("  Del% = Delivery percentage")
    summary.append("  VolDist = % of volume on up days")
    summary.append("  Bonus = Multibagger bonus points")
    
    return "\n".join(summary)


# ============================================================================
# MODIFIED MAIN FUNCTION - Replace your existing main() end
# ============================================================================

def main():
    """
    Modified main function with Smart Money integration
    
    Add this AFTER your existing code, BEFORE the final input() statement
    """
    
    # ... ALL YOUR EXISTING CODE ...
    # (Keep everything as-is until after you push all files to GitHub)
    
    # ... Your existing code pushes files ...
    
    print(f"\nPushed {pushed} files to GitHub.")
    
    # ==================== ADD THIS NEW SECTION ====================
    
    # Run Smart Money Analysis
    results_df, telegram_summary = run_smart_money_analysis_and_push(
        filepath,
        sectors_filepath,
        date_str,
        gh_session,
        headers
    )
    
    if results_df is not None:
        pushed += 2  # CSV + Summary
        
        # Print summary to console
        print("\n" + "="*55)
        print("SMART MONEY SUMMARY (For Telegram):")
        print("="*55)
        print(telegram_summary)
    
    # ==================== END NEW SECTION ====================
    
    if pushed > 0:
        print("\nAll done! GitHub Actions will trigger shortly.")
        print("Check Telegram in 3-5 minutes for your picks.")
    else:
        print("\nAll pushes failed. Check your GitHub token.")

    input("\nPress Enter to exit...")


# ============================================================================
# EXAMPLE: How your modified vcp_scanner.py should look at the end
# ============================================================================

"""
BEFORE (Your current code):
----------------------------
    print(f"\nPushed {pushed} files to GitHub.")
    
    if pushed > 0:
        print("\nAll done! GitHub Actions will trigger shortly.")
    
    input("\nPress Enter to exit...")


AFTER (With Smart Money integration):
--------------------------------------
    print(f"\nPushed {pushed} files to GitHub.")
    
    # NEW: Smart Money Analysis
    results_df, telegram_summary = run_smart_money_analysis_and_push(
        filepath, sectors_filepath, date_str, gh_session, headers
    )
    
    if results_df is not None:
        pushed += 2
        print("\n" + "="*55)
        print("SMART MONEY SUMMARY (For Telegram):")
        print("="*55)
        print(telegram_summary)
    
    if pushed > 0:
        print("\nAll done! GitHub Actions will trigger shortly.")
    
    input("\nPress Enter to exit...")
"""

if __name__ == "__main__":
    print("="*70)
    print("INTEGRATION CODE FOR VCP SCANNER")
    print("="*70)
    print()
    print("This file contains the code to integrate Smart Money Scanner")
    print("with your existing VCP Scanner.")
    print()
    print("INSTRUCTIONS:")
    print("1. Copy the functions above")
    print("2. Paste them into your vcp_scanner.py file")
    print("3. Modify the main() function as shown in the example")
    print("4. Run your VCP scanner as normal")
    print()
    print("="*70)

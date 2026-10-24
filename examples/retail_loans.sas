/* Example: a small retail-loan reporting pipeline (invented data, for demonstration only) */
libname raw  "/data/raw";
libname mart "/data/mart";

%let cutoff   = 20250630;
%let min_bal  = 100;

/* 1. Clean the loan extract */
data work.loans_clean;
    set raw.loan_extract (rename=(bal=balance));
    where balance > &min_bal;
    interest_cost = balance * rate / 12;
    if term_months <= 12 then term_band = 'SHORT';
    else if term_months <= 60 then term_band = 'MEDIUM';
    else term_band = 'LONG';
    keep loan_id customer_id balance interest_cost term_band;
run;

/* 2. Customer attributes */
data work.customers;
    set raw.customer_master;
    full_name = catx(' ', first_name, last_name);
    age_years = intck('year', birth_date, "&cutoff"d);
    keep customer_id full_name age_years region;
run;

/* 3. Join and summarise */
proc sql;
    create table work.region_summary as
    select c.region,
           count(l.loan_id)        as loan_count,
           sum(l.balance)          as total_balance,
           sum(l.interest_cost)    as total_interest_cost,
           avg(c.age_years)        as avg_customer_age
    from work.loans_clean l
         left join work.customers c on l.customer_id = c.customer_id
    group by c.region;

    create table mart.loan_detail as
    select l.loan_id, l.customer_id, c.full_name, l.balance,
           l.interest_cost, l.term_band,
           case when l.balance > 250000 then 'LARGE' else 'STANDARD' end as size_flag
    from work.loans_clean l, work.customers c
    where l.customer_id = c.customer_id;
quit;

/* 4. Final report table */
data mart.region_report;
    set work.region_summary;
    avg_balance = total_balance / loan_count;
    cost_ratio  = total_interest_cost / total_balance;
    keep region loan_count total_balance avg_balance cost_ratio;
run;

-- ============================================================================
-- AVY_FACT_SIDE INSERT FROM SOURCE (DRD-based mapping)
-- Target: TRANSACTIONS_OWNER.AVY_FACT_SIDE
-- Primary Source: CCAL_REPL_OWNER.TXN
-- Generated from DRD_Activity_Fact.xlsx mapping expressions
-- ============================================================================

INSERT INTO TRANSACTIONS_OWNER.AVY_FACT_SIDE (
    SHDW_TXN_TP_CD,
    SHDW_TXN_TP_NM,
    IMP_SRC_ACTN_CD,
    IMP_SRC_ACTN_NM,
    SIS_DLTD_EV_ID,
    SIS_DLTD_EV_NM,
    SIS_USR_ID,
    SIS_AC_CL_CD,
    SRC_CNCL_RSN_ID,
    SRC_CNCL_RSN_CD,
    SRC_CNCL_RSN_NM,
    SRC_TXN_SEQ_NUM,
    DVDN_RCRD_DT,
    OWN_IMP_FA_NUM,
    OWN_FA_NUM,
    OWN_FA_NUM_ENT_CD,
    FA_OWN_EMPE_ID,
    OWN_FA_NUM_ENT_ENTP_ID,
    ACAT_CNTRA_FIRM_CLRG_NUM,
    ACAT_CNTRA_FIRM_NM,
    ACAT_CNTRA_FIRM_SHRT_NM,
    ACAT_CNTRA_FIRM_CLRG_ID_TP_CD,
    MM_ALT_ID,
    AR_ORIG_SRC_STM_CD,
    SRC_STM_ID,
    SRC_STM_NM,
    SRC_CNCL_X_RVSN_CD,
    SRC_OPT_CLSS_CD,
    TXN_RCNCL_REPL_CNT,
    TXN_RCNCL_ST_ID,
    TXN_RCNCL_ST_CD,
    TXN_RCNCL_ST_NM,
    MF_AC_NUM,
    OFST_ORIG_SRC_STM_AR_ID,
    OFST_AR_ORIG_SRC_STM_CD,
    OFST_AR_SETL_TP_CD,
    OFST_AR_SETL_TP_DSC,
    AR_SETL_TP_CD,
    AR_SETL_TP_DSC,
    ORIG_SRC_STM_AR_ID,
    TD_DIM_ID,
    EXG_DIM_ID,
    ACG_TP_CD,
    ACG_TP_DIM_ID,
    ACG_TP_NM,
    ACG_TP_ID,
    ACTV_F,
    AGRT_ORIG_FEES,
    AGRT_STMT_FEES,
    AR_ID,
    BATCH_DT,
    BKG_DT,
    BR_CD,
    BKR_AR_ID,
    BUY_SELL_IND,
    CASH_AVY_CGY,
    CASH_SIMP_DSPL_AVY_CGY,
    CSH_AVY_CL_ID,
    CASH_AVY_TP,
    CASH_SIMP_DSPL_AVY_TP,
    CASH_ALT_DSC_TRAILER_2,
    CASH_APA_EXT_QUALFR,
    CSH_APA_ID,
    CASH_APA_TP_CD,
    CASH_APA_TP_DSC,
    CSH_APA_TP_ID,
    CASH_APA_TP_NM,
    CASH_CIRD_PD_ID,
    CASH_DB_CR_CD,
    CASH_DB_CR_NM,
    CASH_DSC_TRAILER_1,
    CF_GEN_F,
    CASH_POS_TP_CD,
    CASH_POS_TP_DIM_ID,
    CASH_POS_TP_NM,
    CASH_POS_TP_ID,
    CASH_PD_ID,
    CASH_PD_DIM_ID,
    CASH_SRC_SEQ_NUM,
    CASH_SBC_AMT,
    CASH_TXN_AMT,
    CLNT_FRIENDLY_DSC,
    CRT_DTM,
    CRT_USR_NM,
    CCY_CD,
    CCY_DIM_ID,
    DOL_IND_CD,
    DOL_IND_ID,
    DOL_IND_NM,
    EXG_CD,
    EXG_NM,
    EXEC_DTM,
    EXEC_SBTP_CD,
    EXEC_SBTP_DSC,
    EXEC_SBTP_ID,
    EXEC_SBTP_NM,
    EXEC_TP_CD,
    EXEC_TP_DSC,
    EXEC_TP_ID,
    EXEC_TP_NM,
    FA_NUM,
    LGCY_CNCL_CMPLN_RSN_TP_CD,
    LGCY_CNCL_CMPLN_RSN_DIM_ID,
    LGCY_CNCL_CMPLN_RSN_TP_NM,
    LGCY_CNCL_CMPLN_RSN_TP_ID,
    LGCY_CNCL_CMPLN_SRC_TP_CD,
    LGCY_CNCL_CMPLN_SRC_DIM_ID,
    LGCY_CNCL_CMPLN_SRC_TP_NM,
    LGCY_CNCL_CMPLN_SRC_TP_ID,
    LGCY_MKT_TP_CD,
    LGCY_MKT_TP_DIM_ID,
    LGCY_MKT_TP_NM,
    LGCY_MKT_TP_ID,
    LGCY_TRD_CPCTY_TP_CD,
    LGCY_TRD_CPCTY_TP_DIM_ID,
    LGCY_TRD_CPCTY_TP_NM,
    LGCY_TRD_CPCTY_TP_ID,
    OFST_AR_ID,
    OPT_CLSS_CD,
    OMS_EXEC_TP_CD,
    OMS_EXEC_TP_DSC,
    OMS_EXEC_TP_ID,
    OMS_EXEC_TP_NM,
    OMS_ORDR_KEY,
    ORIG_BKG_DT,
    ORIG_TRD_NUM,
    ORIG_TD,
    REL_TXN_SRC_STM_DIM_ID,
    REL_TXN_SRC_STM_ID,
    REL_TXN_SRC_STM_CD,
    REL_TXN_RLTNP_TP_ID,
    REL_TXN_RLTNP_TP_CD,
    REL_TXN_RLTNP_TP_NM,
    SALE_CHRG_RATE_MULTI_NUM,
    SALE_CHRG_RATE_PCT,
    SALE_CHRG_RATE_TP_CD,
    SALE_CHRG_RATE_TP_ID,
    SALE_CHRG_RATE_TP_NM,
    SCR_AVY_CGY,
    SCR_SIMP_DSPL_AVY_CGY,
    SEC_AVY_CL_ID,
    SCR_AVY_TP,
    SCR_SIMP_DSPL_AVY_TP,
    SEC_ALT_DSC_TRAILER_2,
    SEC_APA_EXT_QUALFR,
    SEC_APA_ID,
    SCR_APA_TP_CD,
    SCR_APA_TP_DSC,
    SEC_APA_TP_ID,
    SCR_APA_TP_NM,
    SEC_CIRD_PD_ID,
    SEC_DB_CR_CD,
    SEC_DB_CR_NM,
    SEC_DSC_TRAILER_1,
    SEC_ORIG_QTY,
    SEC_SRC_CUSIP,
    SEC_PD_ID,
    SEC_PD_DIM_ID,
    SEC_PRC_IN_TXN_CCY,
    SEC_SRC_PRC_IN_SBC,
    SEC_SRC_SEQ_NUM,
    SEC_SBC_AMT,
    SEC_TXN_AMT,
    SD,
    SD_DIM_ID,
    SRC_CRT_USRNM,
    SRC_EFF_DT,
    SRC_ENTR_CNL_TP_CD,
    SRC_ENTR_CNL_TP_DIM_ID,
    SRC_ENTR_CNL_TP_NM,
    SRC_ENTR_CNL_TP_ID,
    SRC_ENTR_DTM,
    SRC_PCS_TP_DIM_ID,
    SRC_PCS_TP_ID,
    SRC_PCS_TP_CD,
    SRC_PCS_TP_NM,
    SRC_STM_CD,
    TXN_SRC_STM_DIM_ID,
    SRC_TXN_CODE,
    SBC_CCY_CALC_DT,
    SBC_CCY_CD,
    SBC_CCY_DIM_ID,
    SBC_EXG_RATE,
    REL_TXN_ID,
    TD,
    TRD_NUM,
    TRD_SLCT_TP_DIM_ID,
    TRD_SLCT_TP_ID,
    TRD_SLCT_TP_CD,
    TRD_SLCT_TP_NM,
    TXN_CL_F,
    TXN_ID,
    TXN_SRC_KEY,
    TXN_SUB_TP_CD,
    TXN_SUB_TP_DSC,
    TXN_SBTP_ID,
    TXN_SUB_TP_NM,
    TXN_TP_CD,
    TXN_TP_DSC,
    TXN_TP_ID,
    TXN_TP_NM,
    ORIG_SRC_STM_ID,
    ORIG_SRC_STM_CD,
    BKR_AR_DIM_ID,
    OFST_AR_DIM_ID,
    AR_DIM_ID,
    LAST_UDT_USR_NM,
    LAST_UDT_DTM,
    SESS_NO,
    SCR_OPT_SYMB,
    DRVD_TRD_CPCTY_CD,
    DRVD_TRD_CPCTY_NM,
    DRVD_TRD_CPCTY_ID,
    ADL_TRD_INSR,
    TRD_FCTR_RATE,
    MRKUP_RATE,
    STD_CMSN_AMT,
    ACT_CMSN_AMT,
    TRD_PCS_FEE_AMT,
    SEC_FEE_AMT,
    ACR_INT_AMT,
    CNCSN_AMT,
    CDSC_AMT,
    OTHR_FEE_AMT,
    TRD_CNCLD_F,
    SYMB,
    ISIN,
    TXN_SRC_TAX_CD,
    TXN_SRC_TAX_CD_DSC,
    TM_PRC_DISCRETION_F,
    CASH_NNA_CGY_ID,
    CASH_NNA_CGY_NM,
    SCR_NNA_CGY_ID,
    SCR_NNA_CGY_NM,
    YLD,
    YTW,
    YTW_CD,
    BKR_AC_NUM,
    SIS_DLTD_EV_CD,
    DB_CARD_TXN_DT,
    DB_CARD_ORIG_CCY_CD,
    DB_CARD_ORIG_CCY,
    SDIRA_TXN_TP_CD,
    SDIRA_TXN_TP,
    SDIRA_TXN_YR,
    TXN_CCY,
    BKR_IRA_F,
    BKR_ERISA_F,
    BKR_ORIG_SRC_STM_CD,
    BKR_ABC_CLSS_CD,
    BKR_ABC_CLSS,
    AC_CGY_CD,
    BKR_AC_CGY_CD,
    AC_CGY,
    BKR_AC_CGY,
    BKR_BSN_LINE_AFFLT,
    BKR_BSN_LINE_AFFLT_CD,
    TST_AC_F,
    BKR_TST_AC_F,
    TAX_RPT_CLNT_ID,
    BKR_TAX_RPT_CLNT_ID,
    TAX_AC_F,
    BKR_TAX_AC_F,
    BKR_RJ_TAX_RPT_RSPL_F,
    RJ_BSN_UNIT_CD,
    BKR_RJ_BSN_UNIT_CD,
    RJ_BSN_UNIT,
    BKR_RJ_BSN_UNIT,
    AC_OWNSHP_TP_CD,
    BKR_AC_OWNSHP_TP_CD,
    AC_OWNSHP_TP,
    BKR_AC_OWNSHP_TP,
    BKR_FIRM_AC_F,
    BKR_FEE_BASE_AC_F,
    BKR_RJ_TRUST_F,
    OWN_PRIM_FA_NUM,
    OWN_FA_CSS_PSN_ID,
    OWN_FA_CRN_ADV_F,
    OWN_FA_DEPT_BR_CD,
    OWN_FA_DSCR_F,
    OWN_FA_DSCR_ST,
    OWN_FA_DSCR_ST_CD,
    OWN_FA_FINRA_CRD_CLSS_CD,
    OWN_FA_HR_ST_CD,
    OWN_FA_PRODUCER_F,
    OWN_FA_QUALF_ADV_F,
    OWN_FA_ENT_BR_CD,
    OWN_FA_ENT_BSN_ST,
    OWN_FA_ENT_DIV_CD,
    OWN_FA_ENT_DIV_DSC,
    OWN_FA_ENT_DIV_NODE,
    OWN_FA_ENT_MAIN_BR_STE,
    OWN_FA_ENT_OSJ,
    OWN_FA_ENT_RTL_HIER_BSN_MODL_CD,
    OWN_FA_ENT_RTL_HIER_BSN_MODL_DSC,
    OWN_FA_ENT_RTL_SALE_HIER_LOB_CD,
    OWN_FA_ENT_RTL_SALE_HIER_LOB_DSC,
    OWN_FA_ENT_RTL_HIER_RPT_UNIT_CD,
    OWN_FA_ENT_RTL_HIER_RPT_UNIT_DSC,
    OWN_FA_ENT_RTL_SALE_DIV_CD,
    OWN_FA_ENT_RTL_SALE_HIER_RGON_CD,
    OWN_FA_ENT_RTL_SALE_HIER_RGON_DSC,
    OWN_FA_ENT_RTL_HIER_TERR_CD,
    OWN_FA_ENT_RTL_HIER_TERR_DSC,
    OWN_FA_ENT_RSK_HIER_BSN_UNIT_CD,
    OWN_FA_ENT_RSK_HIER_BSN_UNIT_DSC,
    OWN_FA_ENT_RJAS_ID_F,
    OWN_FA_ENT_SUBDIV_CD,
    OWN_FA_ENT_SUBDIV_DSC,
    OWN_FA_ENT_SUBS,
    OWN_FA_ENT_SBTP,
    OWN_FA_ENT_SBTP_CD,
    OWN_FA_ENT_TP,
    OWN_FA_ENT_TP_CD,
    OWN_FA_ENT_LOB_AB_F,
    OWN_FA_ENT_LOB_AMS_F,
    OWN_FA_ENT_LOB_BSN_MODL,
    OWN_FA_ENT_BSN_OPN_DT,
    OWN_FA_ENT_LONG_CD,
    OWN_FA_ENT_SHRT_CD,
    OWN_FA_ENT_CSS_ID,
    OWN_FA_ENT_RTL_SALE_CPX_CD,
    EQTY_IDY,
    EQTY_IDY_CD,
    EQTY_IDY_GRP,
    EQTY_IDY_GRP_CD,
    EQTY_SECT,
    EQTY_SECT_CD,
    EQTY_SUP_SECT,
    EQTY_SUP_SECT_CD,
    MODL_STRTG_DTL_AST_CLSS,
    MODL_STRTG_DTL_AST_CLSS_CD,
    MODL_STRTG_SMY_AST_CLSS,
    MODL_STRTG_SMY_AST_CLSS_CD,
    PD_CMPOS_DSC,
    PD_SHRT_NM,
    PD_DSC,
    RPT_CL_LVL_1,
    RPT_CL_LVL_2,
    RPT_STRTG_DTL_AST_CLSS,
    RPT_STRTG_DTL_AST_CLSS_CD,
    RPT_STRTG_SMY_AST_CLSS,
    RPT_STRTG_SMY_AST_CLSS_CD,
    FND_FAM,
    SHR_CLSS_TP_CD,
    SHR_CLSS_TP,
    IMT_CL_LVL_1,
    IMT_CL_LVL_1_CD,
    IMT_CL_LVL_2,
    IMT_CL_LVL_2_CD,
    IMT_CL_LVL_3,
    IMT_CL_LVL_3_CD,
    IMT_CL_LVL_4,
    IMT_CL_LVL_4_CD,
    IMT_CL_LVL_5,
    IMT_CL_LVL_5_CD,
    OWN_FA_NUM_TP_CD,
    OWN_FA_NUM_TP,
    BOND_ACRTN_DCN_AMT,
    BOND_AMRZ_PREM_AMT,
    RJ_TRUST_BOOK_VAL_AMT,
    RJ_TRUST_RLZD_TAX_GAIN_OR_LOSS_AMT,
    BFR_REPYMT_FACE_VAL_AMT,
    BFR_INCM_PYMT_FACE_VAL_AMT,
    BASE_POS_QTY,
    NO_SIS_SCR_MVMT_TXN_QTY,
    NO_SIS_SCR_MVMT_TXN_PRC,
    REIVS_SHR_QTY,
    REIVS_PRC
)
SELECT
    t.SRC_TXN_TP,  -- SHDW_TXN_TP_CD
    stt.SRC_TXN_TP_NM,  -- SHDW_TXN_TP_NM
    t.SRC_ACTN_CODE,  -- IMP_SRC_ACTN_CD
    ial2.desc_text,  -- IMP_SRC_ACTN_NM
    t.SRC_BUY_SELL_MULTI_ID,  -- SIS_DLTD_EV_ID
    cv.CL_VAL_NM,  -- SIS_DLTD_EV_NM
    t.USR_ID,  -- SIS_USR_ID
    t.SRC_PRIM_CL,  -- SIS_AC_CL_CD
    t.SRC_CNCL_RSN_ID,  -- SRC_CNCL_RSN_ID
    cv.CL_VAL_CODE,  -- SRC_CNCL_RSN_CD
    cv.CL_VAL_NM,  -- SRC_CNCL_RSN_NM
    t.SRC_TXN_SEQ_NUM,  -- SRC_TXN_SEQ_NUM
    t.DVDN_RCRD_DT,  -- DVDN_RCRD_DT
    t.SALE_PSN_NUM,  -- OWN_IMP_FA_NUM
    ags.FA_NUM,  -- OWN_FA_NUM
    fn.FA_NUMBER_ENTITY_CODE,  -- OWN_FA_NUM_ENT_CD
    fn.RESPONSIBLE_PARTY_EMPLOYEE_ID,  -- FA_OWN_EMPE_ID
    eed.ENTITY_ENTERPRISE_ID,  -- OWN_FA_NUM_ENT_ENTP_ID
    t.ORIG_SRC_STM_CODE,  -- ACAT_CNTRA_FIRM_CLRG_NUM
    ab.BROKER_NAME,  -- ACAT_CNTRA_FIRM_NM
    ab.BROKER_SHORT_NAME,  -- ACAT_CNTRA_FIRM_SHRT_NM
    ab.BROKER_ID_TYPE,  -- ACAT_CNTRA_FIRM_CLRG_ID_TP_CD
    t.ORIG_SRC_STM_CODE,  -- MM_ALT_ID
    ard.ORIG_SRC_STM_CD,  -- AR_ORIG_SRC_STM_CD
    t.SRC_STM_ID,  -- SRC_STM_ID
    ssd.SRC_STM_NM,  -- SRC_STM_NM
    t.SRC_CXL_REV,  -- SRC_CNCL_X_RVSN_CD
    t.SRC_OPT_CLS,  -- SRC_OPT_CLSS_CD
    t.TXN_REPL_CNT,  -- TXN_RCNCL_REPL_CNT
    t.TXN_ST_ID,  -- TXN_RCNCL_ST_ID
    cv.CL_VAL_CODE,  -- TXN_RCNCL_ST_CD
    cv.CL_VAL_NM,  -- TXN_RCNCL_ST_NM
    ard.MF_DRC_EXT_AC_NUM,  -- MF_AC_NUM
    ard.ORIG_SRC_STM_AR_ID,  -- OFST_ORIG_SRC_STM_AR_ID
    ard.ORIG_SRC_STM_CD,  -- OFST_AR_ORIG_SRC_STM_CD
    ard.SETL_TP_CD,  -- OFST_AR_SETL_TP_CD
    ard.SETL_TP,  -- OFST_AR_SETL_TP_DSC
    ard.SETL_TP_CD,  -- AR_SETL_TP_CD
    ard.SETL_TP,  -- AR_SETL_TP_DSC
    ar_.ORIG_SRC_STM_AR_ID
AC_NUM,  -- ORIG_SRC_STM_AR_ID
    dat.DT_DIM_ID,  -- TD_DIM_ID
    exg.EXG_DIM_ID,  -- EXG_DIM_ID
    acg.ACG_TP_CD,  -- ACG_TP_CD
    acg.ACG_TP_DIM_ID,  -- ACG_TP_DIM_ID
    acg.ACG_TP_NM,  -- ACG_TP_NM
    apa.ACG_TP_ID,  -- ACG_TP_ID
    'Y',  -- ACTV_F
    fip.FIP_ORIG_AMT,  -- AGRT_ORIG_FEES
    fip.FIP_STMT_AMT,  -- AGRT_STMT_FEES
    t.AR_ID,  -- AR_ID
    TRUNC(SYSDATE),  -- BATCH_DT
    t.BKG_DT,  -- BKG_DT
    t.BR_CODE,  -- BR_CD
    apa.BKR_AR_ID
AR_ID,  -- BKR_AR_ID
    t.BUY_SELL_IND,  -- BUY_SELL_IND
    avy.AVY_CGY,  -- CASH_AVY_CGY
    avy.DSPL_AVY_CGY,  -- CASH_SIMP_DSPL_AVY_CGY
    txn.AVY_CL_ID,  -- CSH_AVY_CL_ID
    avy.AVY_TP,  -- CASH_AVY_TP
    avy.DSPL_AVY_TP,  -- CASH_SIMP_DSPL_AVY_TP
    apa.ALT_DSC,  -- CASH_ALT_DSC_TRAILER_2
    apa.APA_EXT_QUALFR,  -- CASH_APA_EXT_QUALFR
    apa.APA_ID,  -- CSH_APA_ID
    cv.CL_VAL_CODE,  -- CASH_APA_TP_CD
    cv.DSC,  -- CASH_APA_TP_DSC
    apa.APA_TP_ID,  -- CSH_APA_TP_ID
    cv.CL_VAL_NM,  -- CASH_APA_TP_NM
    cca.CIRD_PD_ID,  -- CASH_CIRD_PD_ID
    cv.CL_VAL_CODE,  -- CASH_DB_CR_CD
    cv.CL_VAL_NM,  -- CASH_DB_CR_NM
    apa.APA_DSC,  -- CASH_DSC_TRAILER_1
    t.CF_GEN_F,  -- CF_GEN_F
    cas.CASH_POS_TP_CD,  -- CASH_POS_TP_CD
    cas.CASH_POS_TP_DIM_ID,  -- CASH_POS_TP_DIM_ID
    cas.CASH_POS_TP_NM,  -- CASH_POS_TP_NM
    apa.CASH_POS_TP_ID,  -- CASH_POS_TP_ID
    apa.PD_ID,  -- CASH_PD_ID
    imt.IMT_PD_DIM_ID,  -- CASH_PD_DIM_ID
    apa.SRC_SEQ_NUM,  -- CASH_SRC_SEQ_NUM
    apa.STM_BASE_CCY_AMT,  -- CASH_SBC_AMT
    apa.TXN_AMT,  -- CASH_TXN_AMT
    apa.CLNT_FRIENDLY_DESC,  -- CLNT_FRIENDLY_DSC
    SYSDATE,  -- CRT_DTM
    'ODI_ETL',  -- CRT_USR_NM
    apa.txn_iso_ccy_code,  -- CCY_CD
    ccy.CCY_DIM_ID,  -- CCY_DIM_ID
    cv.CL_VAL_CODE,  -- DOL_IND_CD
    t.DOL_IND_ID,  -- DOL_IND_ID
    cv.CL_VAL_NM,  -- DOL_IND_NM
    t.EXG_CODE,  -- EXG_CD
    exg.EXG_NM,  -- EXG_NM
    t.EXEC_DTM,  -- EXEC_DTM
    cv.CL_VAL_CODE,  -- EXEC_SBTP_CD
    cv.DSC,  -- EXEC_SBTP_DSC
    t.EXEC_SBTP_ID,  -- EXEC_SBTP_ID
    cv.CL_VAL_NM,  -- EXEC_SBTP_NM
    cv.CL_VAL_CODE,  -- EXEC_TP_CD
    cv.DSC,  -- EXEC_TP_DSC
    t.EXEC_TP_ID,  -- EXEC_TP_ID
    cv.CL_VAL_NM,  -- EXEC_TP_NM
    t.FA_NUM,  -- FA_NUM
    lgc.LGCY_CNCL_CMPLN_RSN_TP_CD,  -- LGCY_CNCL_CMPLN_RSN_TP_CD
    lgc.LGCY_CNCL_CMPLN_RSN_TP_DIM_ID,  -- LGCY_CNCL_CMPLN_RSN_DIM_ID
    lgc.LGCY_CNCL_CMPLN_RSN_TP_NM,  -- LGCY_CNCL_CMPLN_RSN_TP_NM
    t.LGCY_CNCL_CMPLN_RSN_TP_ID,  -- LGCY_CNCL_CMPLN_RSN_TP_ID
    lgc.LGCY_CNCL_CMPLN_SRC_TP_CD,  -- LGCY_CNCL_CMPLN_SRC_TP_CD
    lgc.LGCY_CNCL_CMPLN_SRC_TP_DIM_ID,  -- LGCY_CNCL_CMPLN_SRC_DIM_ID
    lgc.LGCY_CNCL_CMPLN_SRC_TP_NM,  -- LGCY_CNCL_CMPLN_SRC_TP_NM
    t.LGCY_CNCL_CMPLN_SRC_TP_ID,  -- LGCY_CNCL_CMPLN_SRC_TP_ID
    lgc.LGCY_MKT_TP_CD,  -- LGCY_MKT_TP_CD
    lgc.LGCY_MKT_TP_DIM_ID,  -- LGCY_MKT_TP_DIM_ID
    lgc.LGCY_MKT_TP_NM,  -- LGCY_MKT_TP_NM
    t.LGCY_MKT_TP_ID,  -- LGCY_MKT_TP_ID
    lgc.LGCY_TRD_CPCTY_TP_CD
CL_VAL_CODE,  -- LGCY_TRD_CPCTY_TP_CD
    lgc.LGCY_TRD_CPCTY_TP_DIM_ID,  -- LGCY_TRD_CPCTY_TP_DIM_ID
    lgc.LGCY_TRD_CPCTY_TP_NM
CL_VAL_NM,  -- LGCY_TRD_CPCTY_TP_NM
    t.LGCY_TRD_CPCTY_TP_ID,  -- LGCY_TRD_CPCTY_TP_ID
    apa.OFST_AR_ID,  -- OFST_AR_ID
    t.OPTS_CLSS_CODE,  -- OPT_CLSS_CD
    cv.CL_VAL_CODE,  -- OMS_EXEC_TP_CD
    cv.DSC,  -- OMS_EXEC_TP_DSC
    t.OMS_EXEC_TP_ID,  -- OMS_EXEC_TP_ID
    cv.CL_VAL_NM,  -- OMS_EXEC_TP_NM
    t.OMS_ORDR_KEY,  -- OMS_ORDR_KEY
    t.ORIG_BKG_DT,  -- ORIG_BKG_DT
    t.ORIG_TRD_NUM,  -- ORIG_TRD_NUM
    t.ORIG_TD,  -- ORIG_TD
    ssd.SRC_STM_DIM_ID,  -- REL_TXN_SRC_STM_DIM_ID
    t.SRC_STM_ID (from T2),  -- REL_TXN_SRC_STM_ID
    ssd.SRC_STM_CD,  -- REL_TXN_SRC_STM_CD
    tr.TXN_RLTNP_TP_ID,  -- REL_TXN_RLTNP_TP_ID
    cv.CL_VAL_CODE,  -- REL_TXN_RLTNP_TP_CD
    cv.CL_VAL_NM,  -- REL_TXN_RLTNP_TP_NM
    /* Use APACSH logic from 'ETL Notes' tab.

For a Transaction, If 2 APACSH records exists
     If (one r */ NULL,  -- SALE_CHRG_RATE_MULTI_NUM
    /* Use APACSH logic from 'ETL Notes' tab.

For a Transaction, If 2 APACSH records exists
     If (one r */ NULL,  -- SALE_CHRG_RATE_PCT
    cv.CL_VAL_CODE,  -- SALE_CHRG_RATE_TP_CD
    apa.SALE_CHRG_RATE_TP_ID,  -- SALE_CHRG_RATE_TP_ID
    cv.CL_VAL_NM,  -- SALE_CHRG_RATE_TP_NM
    avy.AVY_CGY,  -- SCR_AVY_CGY
    avy.DSPL_AVY_CGY,  -- SCR_SIMP_DSPL_AVY_CGY
    avy.AVY_CL_ID,  -- SEC_AVY_CL_ID
    avy.AVY_TP,  -- SCR_AVY_TP
    avy.DSPL_AVY_TP,  -- SCR_SIMP_DSPL_AVY_TP
    apa.ALT_DSC,  -- SEC_ALT_DSC_TRAILER_2
    apa.APA_EXT_QUALFR,  -- SEC_APA_EXT_QUALFR
    apa.APA_ID,  -- SEC_APA_ID
    cv.CL_VAL_CODE,  -- SCR_APA_TP_CD
    cv.DSC,  -- SCR_APA_TP_DSC
    apa.APA_TP_ID,  -- SEC_APA_TP_ID
    cv.CL_VAL_NM,  -- SCR_APA_TP_NM
    cca.CIRD_PD_ID,  -- SEC_CIRD_PD_ID
    cv.CL_VAL_CODE,  -- SEC_DB_CR_CD
    cv.CL_VAL_NM,  -- SEC_DB_CR_NM
    apa.APA_DSC,  -- SEC_DSC_TRAILER_1
    apa.ORIG_QTY,  -- SEC_ORIG_QTY
    apa.SRC_PD_ID,  -- SEC_SRC_CUSIP
    apa.PD_ID,  -- SEC_PD_ID
    imt.IMT_PD_DIM_ID,  -- SEC_PD_DIM_ID
    apa.SCR_PRC_IN_TXN_CCY,  -- SEC_PRC_IN_TXN_CCY
    apa.SCR_PRC_IN_SBC,  -- SEC_SRC_PRC_IN_SBC
    apa.SRC_SEQ_NUM,  -- SEC_SRC_SEQ_NUM
    apa.STM_BASE_CCY_AMT,  -- SEC_SBC_AMT
    apa.TXN_AMT,  -- SEC_TXN_AMT
    t.SD,  -- SD
    dat.DT_DIM_ID,  -- SD_DIM_ID
    t.SRC_CRT_USRNM,  -- SRC_CRT_USRNM
    apa.SRC_EFF_DT,  -- SRC_EFF_DT
    src.SRC_ENTR_CNL_TP_CD,  -- SRC_ENTR_CNL_TP_CD
    src.SRC_ENTR_CNL_TP_DIM_ID,  -- SRC_ENTR_CNL_TP_DIM_ID
    src.SRC_ENTR_CNL_TP_NM,  -- SRC_ENTR_CNL_TP_NM
    t.SRC_ENTR_CNL_TP_ID,  -- SRC_ENTR_CNL_TP_ID
    t.SRC_ENTR_DTM,  -- SRC_ENTR_DTM
    src.SRC_PCS_TP_DIM_ID,  -- SRC_PCS_TP_DIM_ID
    t.SRC_PCS_TP_ID,  -- SRC_PCS_TP_ID
    src.SRC_PCS_TP_CD,  -- SRC_PCS_TP_CD
    src.SRC_PCS_TP_NM,  -- SRC_PCS_TP_NM
    ssd.SRC_STM_CD,  -- SRC_STM_CD
    ssd.SRC_STM_DIM_ID,  -- TXN_SRC_STM_DIM_ID
    t.SRC_TXN_CODE,  -- SRC_TXN_CODE
    apa.STM_BASE_CCY_CLC_DTM,  -- SBC_CCY_CALC_DT
    apa.STM_BASE_ISO_CCY_CODE,  -- SBC_CCY_CD
    ccy.CCY_DIM_ID,  -- SBC_CCY_DIM_ID
    apa.STM_BASE_CCY_EXG_RATE,  -- SBC_EXG_RATE
    tr.TRGT_TXN_ID,  -- REL_TXN_ID
    t.TD,  -- TD
    t.TRD_NUM,  -- TRD_NUM
    trd.TRD_SLCT_TP_DIM_ID,  -- TRD_SLCT_TP_DIM_ID
    t.TRD_SLCT_TP_ID,  -- TRD_SLCT_TP_ID
    trd.TRD_SLCT_TP_CD,  -- TRD_SLCT_TP_CD
    trd.TRD_SLCT_TP_NM,  -- TRD_SLCT_TP_NM
    t.TXN_CL_F,  -- TXN_CL_F
    t.TXN_ID,  -- TXN_ID
    t.TXN_SRC_KEY,  -- TXN_SRC_KEY
    cv.CL_VAL_CODE,  -- TXN_SUB_TP_CD
    cv.DSC,  -- TXN_SUB_TP_DSC
    t.TXN_SBTP_ID,  -- TXN_SBTP_ID
    cv.CL_VAL_NM,  -- TXN_SUB_TP_NM
    cv.CL_VAL_CODE,  -- TXN_TP_CD
    cv.DSC,  -- TXN_TP_DSC
    t.TXN_TP_ID,  -- TXN_TP_ID
    cv.CL_VAL_NM,  -- TXN_TP_NM
    t.ORIG_SRC_STM_ID,  -- ORIG_SRC_STM_ID
    t.ORIG_SRC_STM_CODE,  -- ORIG_SRC_STM_CD
    ard.AR_DIM_ID,  -- BKR_AR_DIM_ID
    ard.AR_DIM_ID,  -- OFST_AR_DIM_ID
    ard.AR_DIM_ID,  -- AR_DIM_ID
    'ODI_ETL',  -- LAST_UDT_USR_NM
    SYSDATE,  -- LAST_UDT_DTM
    NULL,  -- SESS_NO
    apa.OPT_SYMB,  -- SCR_OPT_SYMB
    cv.CL_VAL_CODE,  -- DRVD_TRD_CPCTY_CD
    cv.CL_VAL_NM,  -- DRVD_TRD_CPCTY_NM
    t.DRVD_TRD_CPCTY_TP_ID,  -- DRVD_TRD_CPCTY_ID
    t.ADL_TRD_INSR,  -- ADL_TRD_INSR
    apa.TRD_FCTR_RATE,  -- TRD_FCTR_RATE
    apa.MRKUP_RATE,  -- MRKUP_RATE
    apa.STD_CMSN_AMT,  -- STD_CMSN_AMT
    fip.STM_BASE_CCY_AMT,  -- ACT_CMSN_AMT
    fip.STM_BASE_CCY_AMT,  -- TRD_PCS_FEE_AMT
    fip.STM_BASE_CCY_AMT,  -- SEC_FEE_AMT
    fip.STM_BASE_CCY_AMT,  -- ACR_INT_AMT
    fip.STM_BASE_CCY_AMT,  -- CNCSN_AMT
    fip.STM_BASE_CCY_AMT
OTHR_FEE,  -- CDSC_AMT
    fip.STM_BASE_CCY_AMT
OTHR_FEE,  -- OTHR_FEE_AMT
    /* If there is a record in CCAL_REPL_OWNER.TXN_RLTNP table with TXN.TXN_ID = TXN_RLTNP.TRGT_TXN_ID
and  */ NULL,  -- TRD_CNCLD_F
    apa.SYMB,  -- SYMB
    apa.ISIN,  -- ISIN
    txn.SRC_TAX_CODE,  -- TXN_SRC_TAX_CD
    txn.SRC_TAX_CODE_DSC,  -- TXN_SRC_TAX_CD_DSC
    NULL,  -- TM_PRC_DISCRETION_F
    txn.NNA_CGY_ID,  -- CASH_NNA_CGY_ID
    nna.NNA_CGY_NM,  -- CASH_NNA_CGY_NM
    txn.NNA_CGY_ID,  -- SCR_NNA_CGY_ID
    nna.NNA_CGY_NM,  -- SCR_NNA_CGY_NM
    apa.YIELD,  -- YLD
    apa.YIELD_TO_WORST,  -- YTW
    apa.YIELD_TO_WORST_CD,  -- YTW_CD
    ar_.orig_src_stm_ar_id,  -- BKR_AC_NUM
    cv.CL_VAL_CODE,  -- SIS_DLTD_EV_CD
    t.ORIG_TD,  -- DB_CARD_TXN_DT
    ccy.CCY_CODE,  -- DB_CARD_ORIG_CCY_CD
    ccy.CCY_NM,  -- DB_CARD_ORIG_CCY
    cv.CL_VAL_CODE,  -- SDIRA_TXN_TP_CD
    cv.CL_VAL_NM,  -- SDIRA_TXN_TP
    t.TRD_NUM,  -- SDIRA_TXN_YR
    ccy.CCY_NM,  -- TXN_CCY
    ar_.IRA_F,  -- BKR_IRA_F
    ar_.ERISSA_F,  -- BKR_ERISA_F
    ar_.ORIG_SRC_STM_CD,  -- BKR_ORIG_SRC_STM_CD
    ar_.ABC_CLSS_CD,  -- BKR_ABC_CLSS_CD
    ar_.ABC_CLSS_NM,  -- BKR_ABC_CLSS
    ar_.ar_cgy_cd,  -- AC_CGY_CD
    ar_.ar_cgy_cd,  -- BKR_AC_CGY_CD
    ar_.ar_cgy,  -- AC_CGY
    ar_.ar_cgy,  -- BKR_AC_CGY
    ar_.bsn_line_afflt,  -- BKR_BSN_LINE_AFFLT
    ar_.bsn_line_afflt_cd,  -- BKR_BSN_LINE_AFFLT_CD
    ar_.tst_ar_f,  -- TST_AC_F
    ar_.tst_ar_f,  -- BKR_TST_AC_F
    ar_.tax_rpt_party_id,  -- TAX_RPT_CLNT_ID
    ar_.tax_rpt_party_id,  -- BKR_TAX_RPT_CLNT_ID
    ar_.tax_ac_f,  -- TAX_AC_F
    ar_.tax_ac_f,  -- BKR_TAX_AC_F
    ar_.rj_tax_rpt_rspl_f,  -- BKR_RJ_TAX_RPT_RSPL_F
    ar_.rj_bsn_unit_cd,  -- RJ_BSN_UNIT_CD
    ar_.rj_bsn_unit_cd,  -- BKR_RJ_BSN_UNIT_CD
    ar_.rj_bsn_unit,  -- RJ_BSN_UNIT
    ar_.rj_bsn_unit,  -- BKR_RJ_BSN_UNIT
    ar_.ownshp_tp_cd,  -- AC_OWNSHP_TP_CD
    ar_.ownshp_tp_cd,  -- BKR_AC_OWNSHP_TP_CD
    ar_.ownshp_tp,  -- AC_OWNSHP_TP
    ar_.ownshp_tp,  -- BKR_AC_OWNSHP_TP
    ar_.firm_ac_f,  -- BKR_FIRM_AC_F
    ar_.fee_base_f,  -- BKR_FEE_BASE_AC_F
    ar_.bsn_line_afflt_cd,  -- BKR_RJ_TRUST_F
    fa_.fa_number,  -- OWN_PRIM_FA_NUM
    per.CSS_PERSON_ID,  -- OWN_FA_CSS_PSN_ID
    per.CURRENT_ADVISOR_FLAG,  -- OWN_FA_CRN_ADV_F
    per.DEPARTMENT_BRANCH_CODE,  -- OWN_FA_DEPT_BR_CD
    per.DISCRETIONARY_INDICATOR,  -- OWN_FA_DSCR_F
    per.DISCRETIONARY_STATUS,  -- OWN_FA_DSCR_ST
    per.DISCRETIONARY_STATUS_CODE,  -- OWN_FA_DSCR_ST_CD
    per.FINRA_CRD_CLASS_CODE,  -- OWN_FA_FINRA_CRD_CLSS_CD
    per.HR_STATUS_CODE,  -- OWN_FA_HR_ST_CD
    per.PRODUCER_FLAG,  -- OWN_FA_PRODUCER_F
    per.QUALIFIED_ADVISOR_FLAG,  -- OWN_FA_QUALF_ADV_F
    ent.entity_branch_code,  -- OWN_FA_ENT_BR_CD
    ent.entity_business_status,  -- OWN_FA_ENT_BSN_ST
    ent.entity_division_code,  -- OWN_FA_ENT_DIV_CD
    ent.entity_division_description,  -- OWN_FA_ENT_DIV_DSC
    ent.entity_division_node,  -- OWN_FA_ENT_DIV_NODE
    ent.entity_main_branch_state,  -- OWN_FA_ENT_MAIN_BR_STE
    ent.entity_osj,  -- OWN_FA_ENT_OSJ
    ent.entity_retail_business_model_code,  -- OWN_FA_ENT_RTL_HIER_BSN_MODL_CD
    ent.entity_retail_business_model_description,  -- OWN_FA_ENT_RTL_HIER_BSN_MODL_DSC
    ent.entity_retail_sales_lob_code,  -- OWN_FA_ENT_RTL_SALE_HIER_LOB_CD
    ent.entity_retail_sales_lob_description,  -- OWN_FA_ENT_RTL_SALE_HIER_LOB_DSC
    ent.entity_retail_reporting_unit_code,  -- OWN_FA_ENT_RTL_HIER_RPT_UNIT_CD
    ent.entity_retail_reporting_unit_description,  -- OWN_FA_ENT_RTL_HIER_RPT_UNIT_DSC
    ent.entity_retail_sales_division_code,  -- OWN_FA_ENT_RTL_SALE_DIV_CD
    ent.entity_retail_sales_region_code,  -- OWN_FA_ENT_RTL_SALE_HIER_RGON_CD
    ent.entity_retail_sales_region_description,  -- OWN_FA_ENT_RTL_SALE_HIER_RGON_DSC
    ent.entity_retail_territory_code,  -- OWN_FA_ENT_RTL_HIER_TERR_CD
    ent.entity_retail_territory_description,  -- OWN_FA_ENT_RTL_HIER_TERR_DSC
    ent.entity_risk_business_unit_code,  -- OWN_FA_ENT_RSK_HIER_BSN_UNIT_CD
    ent.entity_risk_business_unit_description,  -- OWN_FA_ENT_RSK_HIER_BSN_UNIT_DSC
    /* case when own_fa_ent.entity_rjas_id_boolean=1 then 'y' else 'n' end                            as Ow */ NULL,  -- OWN_FA_ENT_RJAS_ID_F
    ent.entity_subdivision_code,  -- OWN_FA_ENT_SUBDIV_CD
    ent.entity_subdivision_description,  -- OWN_FA_ENT_SUBDIV_DSC
    ent.entity_subsidiary,  -- OWN_FA_ENT_SUBS
    ent.entity_subtype,  -- OWN_FA_ENT_SBTP
    ent.entity_subtype_code,  -- OWN_FA_ENT_SBTP_CD
    ent.entity_type,  -- OWN_FA_ENT_TP
    ent.entity_type_code,  -- OWN_FA_ENT_TP_CD
    /* case when own_fa_ent.lob_alex_brown_boolean =1 then 'y' else 'n' end as Owner_FA_entity_lob_alex_bro */ NULL,  -- OWN_FA_ENT_LOB_AB_F
    /* case when own_fa_ent.lob_ams_boolean=1 then 'y' else 'n' end as Owner_FA_entity_lob_ams_flag,

JOIN  */ NULL,  -- OWN_FA_ENT_LOB_AMS_F
    ent.lob_business_model,  -- OWN_FA_ENT_LOB_BSN_MODL
    ent.entity_business_open_date,  -- OWN_FA_ENT_BSN_OPN_DT
    ent.entity_code_long,  -- OWN_FA_ENT_LONG_CD
    ent.entity_code_short,  -- OWN_FA_ENT_SHRT_CD
    ent.entity_css_id,  -- OWN_FA_ENT_CSS_ID
    ent.entity_retail_sales_complex_code,  -- OWN_FA_ENT_RTL_SALE_CPX_CD
    imt.eqty_sect_lvl_4,  -- EQTY_IDY
    imt.eqty_sect_lvl_4_cd,  -- EQTY_IDY_CD
    imt.eqty_sect_lvl_3,  -- EQTY_IDY_GRP
    imt.eqty_sect_lvl_3_cd,  -- EQTY_IDY_GRP_CD
    imt.eqty_sect_lvl_2,  -- EQTY_SECT
    imt.eqty_sect_lvl_2_cd,  -- EQTY_SECT_CD
    imt.eqty_sect_lvl_1,  -- EQTY_SUP_SECT
    imt.eqty_sect_lvl_1_cd,  -- EQTY_SUP_SECT_CD
    imt.firm_modl_lvl_2,  -- MODL_STRTG_DTL_AST_CLSS
    imt.firm_modl_lvl_2_cd,  -- MODL_STRTG_DTL_AST_CLSS_CD
    imt.firm_modl_lvl_1,  -- MODL_STRTG_SMY_AST_CLSS
    imt.firm_modl_lvl_1_cd,  -- MODL_STRTG_SMY_AST_CLSS_CD
    apa.PD_CMPOS_DSC,  -- PD_CMPOS_DSC
    imt.pd_nm,  -- PD_SHRT_NM
    imt.pd_dsc,  -- PD_DSC
    imt.rpt_cl_lvl_1,  -- RPT_CL_LVL_1
    imt.rpt_cl_lvl_2,  -- RPT_CL_LVL_2
    imt.firm_rpt_lvl_2,  -- RPT_STRTG_DTL_AST_CLSS
    imt.firm_rpt_lvl_2_cd,  -- RPT_STRTG_DTL_AST_CLSS_CD
    imt.firm_rpt_lvl_1,  -- RPT_STRTG_SMY_AST_CLSS
    imt.firm_rpt_lvl_1_cd,  -- RPT_STRTG_SMY_AST_CLSS_CD
    imt.fnd_fam,  -- FND_FAM
    imt.shr_clss_tp_cd,  -- SHR_CLSS_TP_CD
    imt.shr_clss_tp_nm,  -- SHR_CLSS_TP
    imt.imt_cl_lvl_1,  -- IMT_CL_LVL_1
    imt.imt_cl_lvl_1_cd,  -- IMT_CL_LVL_1_CD
    imt.imt_cl_lvl_2,  -- IMT_CL_LVL_2
    imt.imt_cl_lvl_2_cd,  -- IMT_CL_LVL_2_CD
    imt.imt_cl_lvl_3,  -- IMT_CL_LVL_3
    imt.imt_cl_lvl_3_cd,  -- IMT_CL_LVL_3_CD
    imt.imt_cl_lvl_4,  -- IMT_CL_LVL_4
    imt.imt_cl_lvl_4_cd,  -- IMT_CL_LVL_4_CD
    imt.imt_cl_lvl_5,  -- IMT_CL_LVL_5
    imt.imt_cl_lvl_5_cd,  -- IMT_CL_LVL_5_CD
    fn.FA_NUMBER_TYPE_CODE,  -- OWN_FA_NUM_TP_CD
    cod.CODE_VALUE_DESCRIPTION,  -- OWN_FA_NUM_TP
    apa.ORIG_QTY,  -- BOND_ACRTN_DCN_AMT
    apa.ORIG_QTY,  -- BOND_AMRZ_PREM_AMT
    apa.ORIG_QTY,  -- RJ_TRUST_BOOK_VAL_AMT
    apa.ORIG_QTY,  -- RJ_TRUST_RLZD_TAX_GAIN_OR_LOSS_AMT
    apa.ORIG_QTY,  -- BFR_REPYMT_FACE_VAL_AMT
    apa.ORIG_QTY,  -- BFR_INCM_PYMT_FACE_VAL_AMT
    apa.ORIG_QTY,  -- BASE_POS_QTY
    apa.ORIG_QTY,  -- NO_SIS_SCR_MVMT_TXN_QTY
    apa.SCR_PRC_IN_TXN_CCY,  -- NO_SIS_SCR_MVMT_TXN_PRC
    apa.ORIG_QTY,  -- REIVS_SHR_QTY
    apa.SCR_PRC_IN_TXN_CCY  -- REIVS_PRC
FROM CCAL_REPL_OWNER.TXN t

-- === JOINs (derived from DRD transformation rules) ===
LEFT JOIN CCAL_REPL_OWNER.ACATS_BROKER ab ON ab.BROKER_ID = t.CNTRA_BROKER_ID
-- LEFT JOIN CCAL_REPL_OWNER.APA apa ON ???  -- TODO: determine join condition
-- LEFT JOIN CCAL_REPL_OWNER.APA
TXN apa ON ???  -- TODO: determine join condition
-- LEFT JOIN CCAL_REPL_OWNER.AVY_CL avy ON ???  -- TODO: determine join condition
-- LEFT JOIN CCAL_REPL_OWNER.CCAL_CIRD_PD_MAP cca ON ???  -- TODO: determine join condition
LEFT JOIN CCAL_REPL_OWNER.CL_VAL cv ON cv.CL_VAL_ID = t.SRC_BUY_SELL_MULTI_ID
-- LEFT JOIN CCAL_REPL_OWNER.FIP fip ON ???  -- TODO: determine join condition
-- LEFT JOIN CCAL_REPL_OWNER.J$TXN j$t ON ???  -- TODO: determine join condition
-- LEFT JOIN CCAL_REPL_OWNER.NNA_CGY nna ON ???  -- TODO: determine join condition
LEFT JOIN CCAL_REPL_OWNER.SHDW_TXN_TP stt ON stt.SRC_TXN_TP = t.SRC_TXN_TP
-- LEFT JOIN CCAL_REPL_OWNER.TXN_AVY_CL txn ON ???  -- TODO: determine join condition
LEFT JOIN CCAL_REPL_OWNER.TXN_RLTNP tr ON tr.TXN_ID = t.TXN_ID
-- LEFT JOIN CCAL_REPL_OWNER.TXN_SRC_TAX_CODE_LKUP txn ON ???  -- TODO: determine join condition
-- LEFT JOIN CCAL_REPL_OWNER.ccal_cird_pd_map cca ON ???  -- TODO: determine join condition
LEFT JOIN CCSI_OWNER.AR_DIM ard ON ard.AR_ID = t.AR_ID AND ard.ACTV_F = 'Y'
-- LEFT JOIN CCSI_OWNER.AR_DIM
AR_AC_SUBDIM ar_ ON ???  -- TODO: determine join condition
LEFT JOIN CCSI_OWNER.AR_GRP_SUBDIM ags ON ags.AR_ID = t.AR_ID AND ags.ACTV_F = 'Y'
-- LEFT JOIN CCSI_OWNER.ar_dim ar_ ON ???  -- TODO: determine join condition
-- LEFT JOIN CIRD_OWNER.CCY_DIM ccy ON ???  -- TODO: determine join condition
-- LEFT JOIN CIRD_OWNER.EXG_DIM exg ON ???  -- TODO: determine join condition
-- LEFT JOIN CIRD_OWNER.IMT_PD_DIM imt ON ???  -- TODO: determine join condition
-- LEFT JOIN COMMON_OWNER.ACG_TP_DIM acg ON ???  -- TODO: determine join condition
-- LEFT JOIN COMMON_OWNER.CASH_POS_TP_DIM cas ON ???  -- TODO: determine join condition
-- LEFT JOIN COMMON_OWNER.DATE_DIM dat ON ???  -- TODO: determine join condition
LEFT JOIN COMMON_OWNER.SRC_STM_DIM ssd ON ssd.SRC_STM_ID = t.SRC_STM_ID
LEFT JOIN REFERENCE_REPL_OWNER.IMPCT_ACTION_LKU ial2 ON ial2.IMPCT_ACTION_ID = t.IMPCT_ACTION_ID
-- LEFT JOIN Reference_Repl_Owner.CCY ccy ON ???  -- TODO: determine join condition
-- LEFT JOIN SSDS_DAL_OWNER.CODE_SET_VALUE_V cod ON ???  -- TODO: determine join condition
LEFT JOIN SSDS_DAL_OWNER.ENTERPRISE_ENTITY_DIM_V eed ON eed.ENTITY_CODE = fn.FA_NUMBER_ENTITY_CODE
-- LEFT JOIN SSDS_DAL_OWNER.ENTERPRISE_ENTITY_RISK_DIM ent ON ???  -- TODO: determine join condition
LEFT JOIN SSDS_DAL_OWNER.FA_NUMBER_V fn ON fn.FA_NUMBER = ags.FA_NUM
-- LEFT JOIN SSDS_DAL_OWNER.PERSON_AMS_DISCRETIONARY_STATUS_V per ON ???  -- TODO: determine join condition
-- LEFT JOIN SSDS_DAL_OWNER.PERSON_RV per ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER.LGCY_CNCL_CMPLN_RSN_TP_DIM lgc ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER.LGCY_CNCL_CMPLN_SRC_TP_DIM lgc ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER.LGCY_MKT_TP_DIM lgc ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER.LGCY_TRD_CPCTY_TP_DIM lgc ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER.SRC_ENTR_CNL_TP_DIM src ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER.SRC_PCS_TP_DIM src ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER.TRD_SLCT_TP_DIM trd ON ???  -- TODO: determine join condition
-- LEFT JOIN TRANSACTIONS_OWNER
CCAL_REPL_OWNER.LGCY_TRD_CPCTY_TP_DIM
CL_VAL lgc ON ???  -- TODO: determine join condition
-- LEFT JOIN cird_owner.IMT_PD_DIM imt ON ???  -- TODO: determine join condition
-- LEFT JOIN cird_owner.imt_pd_dim imt ON ???  -- TODO: determine join condition
-- LEFT JOIN ssds_dal_owner.ENTERPRISE_ENTITY_DIM_V ent ON ???  -- TODO: determine join condition
-- LEFT JOIN ssds_dal_owner.ENTERPRISE_ENTITY_RETAIL_DIMENSION_V ent ON ???  -- TODO: determine join condition
-- LEFT JOIN ssds_dal_owner.ENTERPRISE_ENTITY_RISK_DIM ent ON ???  -- TODO: determine join condition
-- LEFT JOIN ssds_dal_owner.PERSON_BROKERAGE_SUBDIMENSION_V per ON ???  -- TODO: determine join condition
-- LEFT JOIN ssds_dal_owner.fa_number_v fa_ ON ???  -- TODO: determine join condition

WHERE 1=1
    -- AND t.BATCH_DT = TRUNC(SYSDATE)  -- Filter for current batch
;
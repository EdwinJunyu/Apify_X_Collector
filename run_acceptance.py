if __name__ == '__main__':
    import argparse,json
    from pathlib import Path
    from collector.acceptance import offline_trial,classification_sample,assess_live_evidence
    from collector.storage import atomic_bytes
    from collector.validation import canonical
    parser=argparse.ArgumentParser(description='M8 Mock acceptance; never initiates live collection')
    parser.add_argument('--count',type=int,choices=(5,20,50,150),default=5)
    parser.add_argument('--output',default='acceptance-report.json')
    parser.add_argument('--review-live-report')
    args=parser.parse_args()
    if args.review_live_report:
        report=assess_live_evidence(json.loads(Path(args.review_live_report).read_text(encoding='utf-8')))
    else:
        report=offline_trial(args.count);report['classification_sample']=classification_sample()
    atomic_bytes(Path(args.output).absolute(),canonical(report))
    print(json.dumps(report,ensure_ascii=False,indent=2))

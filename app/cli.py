"""Run: python -m app.cli --help. Credentials come only from .env/environment."""
import argparse
import json
import os
import urllib.request
from dotenv import load_dotenv

def main():
    load_dotenv()
    parser=argparse.ArgumentParser(description='Control the enrichment service')
    parser.add_argument('--url',default=os.getenv('PUBLIC_BASE_URL','http://localhost:8000'))
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('setup'); sub.add_parser('jobs')
    show=sub.add_parser('job'); show.add_argument('id')
    retry=sub.add_parser('retry'); retry.add_argument('id')
    company=sub.add_parser('company'); company.add_argument('id',type=int)
    company.add_argument('--titles',default=os.getenv('DEFAULT_JOB_TITLES','engineer'))
    company.add_argument('--limit',type=int,default=5)
    company.add_argument('--discover',action='store_true')
    contact=sub.add_parser('contact'); contact.add_argument('id',type=int)
    args=parser.parse_args()
    paths={'setup':('POST','/admin/setup'),'jobs':('GET','/admin/jobs'),
           'job':('GET',f'/admin/jobs/{getattr(args,"id","")}'),
           'retry':('POST',f'/admin/jobs/{getattr(args,"id","")}/retry'),
           'company':('POST',f'/admin/companies/{getattr(args,"id","")}/enrich'),
           'contact':('POST',f'/admin/contacts/{getattr(args,"id","")}/enrich')}
    method,path=paths[args.command]
    payload={'titles':args.titles,'limit':args.limit,'discover':args.discover} if args.command=='company' else {}
    data=json.dumps(payload).encode() if method=='POST' else None
    key=os.getenv('ADMIN_API_KEY','')
    if not key: parser.error('Set ADMIN_API_KEY in .env')
    request=urllib.request.Request(args.url.rstrip('/')+path,data=data,method=method,
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=60) as response:
        print(json.dumps(json.load(response),indent=2))

if __name__=='__main__': main()

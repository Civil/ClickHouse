#!/usr/bin/env python3

import requests
import argparse
import jwt
import sys
import json
import time
from collections import namedtuple

def get_key_and_app_from_aws():
    import boto3
    secret_name = "clickhouse_github_secret_key"
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
    )
    get_secret_value_response = client.get_secret_value(
        SecretId=secret_name
    )
    data = json.loads(get_secret_value_response['SecretString'])
    return data['clickhouse-app-key'], int(data['clickhouse-app-id'])

def handler(event, context):
    private_key, app_id = get_key_and_app_from_aws()
    main(private_key, app_id, True)

def get_installation_id(jwt_token):
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.get("https://api.github.com/app/installations", headers=headers)
    response.raise_for_status()
    data = response.json()
    return data[0]['id']

def get_access_token(jwt_token, installation_id):
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.post(f"https://api.github.com/app/installations/{installation_id}/access_tokens", headers=headers)
    response.raise_for_status()
    data = response.json()
    return data['token']


RunnerDescription = namedtuple('RunnerDescription', ['id', 'name', 'tags', 'offline', 'busy'])

def list_runners(access_token):
    headers = {
        "Authorization": f"token {access_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    response = requests.get("https://api.github.com/orgs/ClickHouse/actions/runners", headers=headers)
    response.raise_for_status()
    data = response.json()
    print("Total runners", data['total_count'])
    runners = data['runners']
    result = []
    for runner in runners:
        tags = [tag['name'] for tag in runner['labels']]
        desc = RunnerDescription(id=runner['id'], name=runner['name'], tags=tags,
                                 offline=runner['status']=='offline', busy=runner['busy'])
        result.append(desc)
    return result

def push_metrics_to_cloudwatch(listed_runners, namespace):
    import boto3
    client = boto3.client('cloudwatch')
    metrics_data = []
    busy_runners = sum(1 for runner in listed_runners if runner.busy)
    metrics_data.append({
        'MetricName': 'BusyRunners',
        'Value': busy_runners,
        'Unit': 'Count',
    })
    total_active_runners = sum(1 for runner in listed_runners if not runner.offline)
    metrics_data.append({
        'MetricName': 'ActiveRunners',
        'Value': total_active_runners,
        'Unit': 'Count',
    })
    total_runners = len(listed_runners)
    metrics_data.append({
        'MetricName': 'TotalRunners',
        'Value': total_runners,
        'Unit': 'Count',
    })
    if total_active_runners == 0:
        busy_ratio = 100
    else:
        busy_ratio = busy_runners / total_active_runners * 100

    metrics_data.append({
        'MetricName': 'BusyRunnersRatio',
        'Value': busy_ratio,
        'Unit': 'Percent',
    })

    client.put_metric_data(Namespace='RunnersMetrics', MetricData=metrics_data)

def main(github_secret_key, github_app_id, push_to_cloudwatch):
    payload = {
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) + (10 * 60),
        "iss": github_app_id,
    }

    encoded_jwt = jwt.encode(payload, github_secret_key, algorithm="RS256")
    installation_id = get_installation_id(encoded_jwt)
    access_token = get_access_token(encoded_jwt, installation_id)
    runners = list_runners(access_token)
    if push_to_cloudwatch:
        push_metrics_to_cloudwatch(runners, 'RunnersMetrics')
    else:
        print(runners)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Get list of runners and their states')
    parser.add_argument('-p', '--private-key-path', help='Path to file with private key')
    parser.add_argument('-k', '--private-key', help='Private key')
    parser.add_argument('-a', '--app-id', type=int, help='GitHub application ID', required=True)
    parser.add_argument('--push-to-cloudwatch', action='store_true',  help='Store received token in parameter store')

    args = parser.parse_args()

    if not args.private_key_path and not args.private_key:
        print("Either --private-key-path or --private-key must be specified", file=sys.stderr)

    if args.private_key_path and args.private_key:
        print("Either --private-key-path or --private-key must be specified", file=sys.stderr)

    if args.private_key:
        private_key = args.private_key
    else:
        with open(args.private_key_path, 'r') as key_file:
            private_key = key_file.read()

    main(private_key, args.app_id, args.push_to_cloudwatch)

#!/usr/bin/env python3
"""Render a local, Git-tracked Kustomize tree. Never contacts a cluster."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {p.stem for p in (ROOT / 'runtime/captures').glob('*.sh')}
INVENTORY = '.generated.json'


def dump(value):
    return yaml.safe_dump(value, sort_keys=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def generate(config_path):
    config_path = Path(config_path).resolve()
    config = yaml.safe_load(config_path.read_text())
    if not isinstance(config, dict) or set(config) != {'schema', 'image', 'services'} or config['schema'] != 1:
        raise ValueError('Expected schema: 1, image and services')
    image = config['image']
    if not isinstance(image, str) or not image or any(c.isspace() for c in image):
        raise ValueError('image must be an explicit image reference')
    if not isinstance(config['services'], dict) or not config['services']:
        raise ValueError('services must be a nonempty mapping')
    image_override = {'name': 'osixia/backup'}
    if '@' in image:
        repository, checksum = image.split('@', 1)
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', checksum):
            raise ValueError('Expected a sha256 image digest')
        if ':' in repository.rsplit('/', 1)[-1]:
            repository = repository.rsplit(':', 1)[0]
        image_override.update(newName=repository, digest=checksum)
    else:
        if ':' not in image.rsplit('/', 1)[-1]:
            raise ValueError('An explicit image tag or digest is required')
        repository, tag = image.rsplit(':', 1)
        if not repository or not tag:
            raise ValueError('An explicit image tag or digest is required')
        image_override.update(newName=repository, newTag=tag)
    files = {}
    for p in sorted((ROOT / 'kubernetes/base').iterdir()):
        if p.is_file():
            files['common/' + p.name] = p.read_bytes()
    services = []
    for name, service in config['services'].items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z]+', name) or name == 'common':
            raise ValueError('Service names must contain lowercase letters only, excluding common')
        if not isinstance(service, dict) or set(service) - {'profile', 'namespace', 'settings', 'overlay', 'files', 'capture'}:
            raise ValueError(f'{name}: unknown service field')
        profile = service.get('profile')
        if profile not in PROFILES:
            raise ValueError(f'{name}: unknown profile')
        namespace = service.get('namespace')
        if not isinstance(namespace, str) or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?', namespace):
            raise ValueError(f'{name}: invalid namespace')
        settings = service.get('settings', {})
        if not isinstance(settings, dict) or 'SERVICE' in settings:
            raise ValueError(f'{name}: settings must be a mapping without SERVICE')
        settings = dict(settings)
        if profile in {'mail', 'nextcloud', 'sqlite-offline'}:
            application = settings.get('APPLICATION')
            if not isinstance(application, str) or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?', application):
                raise ValueError(f'{name}: APPLICATION must identify the workload')
            if settings.get('STATE_CONFIGMAP', name + '-backups-state') != name + '-backups-state':
                raise ValueError(f'{name}: STATE_CONFIGMAP is generated from the service name')
            settings['STATE_CONFIGMAP'] = name + '-backups-state'
        literals = [f'SERVICE={name}']
        for key, value in settings.items():
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, (str, int)) or '\n' in str(value):
                raise ValueError(f'{name}: invalid setting {key}')
            literals.append(f'{key}={value}')
        overlay = service.get('overlay', {})
        if not isinstance(overlay, dict) or set(overlay) - {'resources', 'patches', 'configMapGenerator', 'labels', 'replacements'}:
            raise ValueError(f'{name}: unsupported overlay field')
        kustomization = {
            'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
            'namespace': namespace, 'namePrefix': name + '-',
            'resources': ['../common'] + overlay.get('resources', []),
            'images': [image_override],
            'configMapGenerator': [
                {'name': 'backups-settings', 'behavior': 'merge', 'literals': literals},
                {'name': 'backups-scripts', 'files': ['capture.sh']},
            ] + overlay.get('configMapGenerator', []),
        }
        kustomization['labels'] = [{'pairs': {'backup.osixia.net/service': name}, 'includeSelectors': True}] + overlay.get('labels', [])
        for key in ('patches', 'replacements'):
            if key in overlay:
                kustomization[key] = overlay[key]
        files[f'{name}/capture.sh'] = (ROOT / 'runtime/captures' / (profile + '.sh')).read_bytes()
        if profile in {'mail', 'nextcloud', 'sqlite-offline'}:
            control_profile = 'mail' if profile == 'mail' else 'deployment'
            control = (ROOT / 'kubernetes/profiles' / control_profile / 'control.yaml').read_text()
            control = control.replace('@@APPLICATION@@', settings['APPLICATION']).replace('@@STATE_CONFIGMAP@@', settings['STATE_CONFIGMAP'])
            files[f'{name}/control.yaml'] = control.encode()
            files[f'{name}/kubeconfig.yaml'] = (ROOT / 'kubernetes/kubeconfig.yaml').read_bytes()
            kustomization['configMapGenerator'].append({'name': 'backups-kubeconfig', 'files': ['config=kubeconfig.yaml']})
            kustomization['resources'].append('control.yaml')
            auth_patch = {'apiVersion': 'batch/v1', 'kind': 'CronJob', 'metadata': {'name': 'backups'},
                          'spec': {'jobTemplate': {'spec': {'template': {'spec': {
                              'serviceAccountName': 'backups', 'automountServiceAccountToken': True,
                              'containers': [{'name': 'backup',
                                  'env': [{'name': 'KUBECONFIG', 'value': '/kube/config'}],
                                  'volumeMounts': [{'name': 'kubeconfig', 'mountPath': '/kube', 'readOnly': True}]}],
                              'volumes': [{'name': 'kubeconfig', 'configMap': {'name': 'backups-kubeconfig'}}]}}}}}}
            kustomization['patches'] = [{'patch': dump(auth_patch).decode()}] + kustomization.get('patches', [])
        if 'capture' in service:
            capture_path = (config_path.parent / service['capture']).resolve()
            if not capture_path.is_relative_to(config_path.parent):
                raise ValueError(f'{name}: capture must stay inside the configuration directory')
            files[f'{name}/capture.sh'] = capture_path.read_bytes()
        supplied = service.get('files', {})
        if not isinstance(supplied, dict):
            raise ValueError(f'{name}: files must map destination filenames to project source paths')
        for target, source in supplied.items():
            if not isinstance(target, str) or '/' in target or target in {'', '.', '..', 'kustomization.yaml', 'capture.sh', 'control.yaml', 'kubeconfig.yaml'}:
                raise ValueError(f'{name}: invalid destination filename')
            source_path = (config_path.parent / source).resolve()
            if not source_path.is_relative_to(config_path.parent):
                raise ValueError(f'{name}: source files must stay inside the configuration directory')
            files[f'{name}/{target}'] = source_path.read_bytes()
        references = list(overlay.get('resources', []))
        references += [patch['path'] for patch in overlay.get('patches', []) if 'path' in patch]
        for reference in references:
            if reference not in supplied:
                raise ValueError(f'{name}: resource and patch paths must be local supplied files')
        files[f'{name}/kustomization.yaml'] = dump(kustomization)
        services.append(name)
    files['kustomization.yaml'] = dump({
        'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
        'resources': services,
    })
    output = config_path.parent / 'generated'
    if output.is_symlink():
        raise ValueError('Refusing a symlink output directory')
    old = {}
    if output.exists():
        for p in output.rglob('*'):
            if p.is_symlink():
                raise ValueError('Refusing symlinks in generated output')
        inventory = output / INVENTORY
        if not inventory.is_file():
            raise ValueError('Existing output has no generator inventory')
        old = json.loads(inventory.read_text())['files']
        for path, expected in old.items():
            p = output / path
            if not p.resolve().is_relative_to(output.resolve()) or not p.is_file() or digest(p.read_bytes()) != expected:
                raise ValueError('Generated files were edited or removed; move changes into project inputs before regenerating')
        existing = {str(p.relative_to(output)) for p in output.rglob('*') if p.is_file()}
        if existing != set(old) | {INVENTORY}:
            raise ValueError('Untracked files in generated output; move them into project inputs')
    # Validate and render everything before changing the output.
    for path, data in files.items():
        p = output / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for path in old.keys() - files.keys():
        (output / path).unlink()
    metadata = {'schema': 1, 'files': {p: digest(data) for p, data in sorted(files.items())}}
    (output / INVENTORY).write_text(json.dumps(metadata, indent=2) + '\n')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    try:
        print(generate(args.config))
    except (ValueError, TypeError, KeyError, IndexError, OSError, yaml.YAMLError) as error:
        sys.exit(str(error))

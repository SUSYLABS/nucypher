#!/usr/bin/env python3
import json
import logging
import os
import random
import sys

import click
import collections
import shutil
import subprocess
from constant_sorrow import constants
from cryptography.hazmat.primitives.asymmetric import ec
from eth_utils import is_checksum_address
from twisted.internet import reactor
from typing import Tuple, ClassVar
from web3.middleware import geth_poa_middleware

from nucypher.blockchain.eth.actors import Miner
from nucypher.blockchain.eth.agents import MinerAgent, PolicyAgent, NucypherTokenAgent, EthereumContractAgent
from nucypher.blockchain.eth.chains import Blockchain
from nucypher.blockchain.eth.constants import (DISPATCHER_SECRET_LENGTH,
                                               MIN_ALLOWED_LOCKED,
                                               MIN_LOCKED_PERIODS,
                                               MAX_MINTING_PERIODS)
from nucypher.blockchain.eth.deployers import NucypherTokenDeployer, MinerEscrowDeployer, PolicyManagerDeployer
from nucypher.blockchain.eth.interfaces import BlockchainDeployerInterface
from nucypher.blockchain.eth.registry import TemporaryEthereumContractRegistry
from nucypher.blockchain.eth.sol.compile import SolidityCompiler
from nucypher.config.characters import UrsulaConfiguration
from nucypher.config.constants import BASE_DIR
from nucypher.config.keyring import NucypherKeyring
from nucypher.config.node import NodeConfiguration
from nucypher.config.utils import validate_configuration_file, generate_local_wallet, generate_account
from nucypher.crypto.api import generate_self_signed_certificate, _save_tls_certificate
from nucypher.utilities.sandbox.blockchain import TesterBlockchain, token_airdrop
from nucypher.utilities.sandbox.constants import (DEVELOPMENT_TOKEN_AIRDROP_AMOUNT,
                                                  DEVELOPMENT_ETH_AIRDROP_AMOUNT,
                                                  )
from nucypher.utilities.sandbox.ursula import UrsulaProcessProtocol

__version__ = '0.1.0-alpha.0'

BANNER = """
                                  _               
                                 | |              
     _ __  _   _  ___ _   _ _ __ | |__   ___ _ __ 
    | '_ \| | | |/ __| | | | '_ \| '_ \ / _ \ '__|
    | | | | |_| | (__| |_| | |_) | | | |  __/ |   
    |_| |_|\__,_|\___|\__, | .__/|_| |_|\___|_|   
                       __/ | |                    
                      |___/|_|      
                                    
    version {}

""".format(__version__)


#
# Setup Logging
#

root = logging.getLogger()
root.setLevel(logging.DEBUG)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
root.addHandler(ch)


#
# CLI Configuration
#


class NucypherClickConfig:

    def __init__(self):

        # Node Configuration
        self.node_configuration = constants.NO_NODE_CONFIGURATION
        self.dev = constants.NO_NODE_CONFIGURATION
        self.federated_only = constants.NO_NODE_CONFIGURATION
        self.config_root = constants.NO_NODE_CONFIGURATION
        self.config_file = constants.NO_NODE_CONFIGURATION

        # Blockchain
        self.deployer = constants.NO_BLOCKCHAIN_CONNECTION
        self.compile = constants.NO_BLOCKCHAIN_CONNECTION
        self.poa = constants.NO_BLOCKCHAIN_CONNECTION
        self.blockchain = constants.NO_BLOCKCHAIN_CONNECTION
        self.provider_uri = constants.NO_BLOCKCHAIN_CONNECTION
        self.registry_filepath = constants.NO_BLOCKCHAIN_CONNECTION
        self.accounts = constants.NO_BLOCKCHAIN_CONNECTION

        # Agency
        self.token_agent = constants.NO_BLOCKCHAIN_CONNECTION
        self.miner_agent = constants.NO_BLOCKCHAIN_CONNECTION
        self.policy_agent = constants.NO_BLOCKCHAIN_CONNECTION

        # Simulation
        self.sim_processes = constants.NO_SIMULATION_RUNNING

    def get_node_configuration(self):
        if self.config_root:
            node_configuration = NodeConfiguration(temp=False,
                                                   config_root=self.config_root,
                                                   auto_initialize=False)
        elif self.dev:
            node_configuration = NodeConfiguration(temp=self.dev, auto_initialize=False)
        elif self.config_file:
            click.echo("Using configuration file at: {}".format(self.config_file))
            node_configuration = NodeConfiguration.from_configuration_file(filepath=self.config_file)
        else:
            node_configuration = NodeConfiguration(auto_initialize=False)  # Fully Default

        self.node_configuration = node_configuration

    def connect_to_blockchain(self):
        if self.federated_only:
            raise NodeConfiguration.ConfigurationError("Cannot connect to blockchain in federated mode")
        if self.deployer:
            self.registry_filepath = NodeConfiguration.REGISTRY_SOURCE
        if self.compile:
            click.confirm("Compile solidity source?", abort=True)
        self.blockchain = Blockchain.connect(provider_uri=self.provider_uri,
                                             registry_filepath=self.registry_filepath or self.node_configuration.registry_filepath,
                                             deployer=self.deployer,
                                             compile=self.compile)
        if self.poa:
            w3 = self.blockchain.interface.w3
            w3.middleware_stack.inject(geth_poa_middleware, layer=0)
        self.accounts = self.blockchain.interface.w3.eth.accounts

    def connect_to_contracts(self) -> None:
        """Initialize contract agency and set them on config"""
        self.token_agent = NucypherTokenAgent(blockchain=self.blockchain)
        self.miner_agent = MinerAgent(token_agent=self.token_agent)
        self.policy_agent = PolicyAgent(miner_agent=self.miner_agent)

    def create_account(self) -> str:
        """Creates a new local or hosted ethereum wallet"""
        choice = click.prompt("Create a new Hosted or Local account?", default='hosted', type=str).strip().lower()
        if choice not in ('hosted', 'local'):
            click.echo("Invalid Input")
            raise click.Abort()

        passphrase = click.prompt("Enter a passphrase to encrypt your wallet's private key")
        passphrase_confirmation = click.prompt("Confirm passphrase to encrypt your wallet's private key")
        if passphrase != passphrase_confirmation:
            click.echo("Passphrases did not match")
            raise click.Abort()

        if choice == 'local':
            keyring = generate_local_wallet(passphrase=passphrase, keyring_root=self.node_configuration.keyring_dir)
            new_address = keyring.transacting_public_key
        elif choice == 'hosted':
            new_address = generate_account(w3=self.blockchain.interface.w3, passphrase=passphrase)
        else:
            raise click.Abort()
        return new_address

    def create_node_tls_certificate(self, common_name: str, full_filepath: str) -> None:
        days = click.prompt("How many days do you want the certificate to remain valid? (365 is default)",
                            default=365,
                            type=int)  # TODO: Perhaps make this equal to the stake length?

        host = click.prompt("Enter the node's hostname", default='localhost')  # TODO: remove localhost as default

        # TODO: save TLS private key
        certificate, private_key = generate_self_signed_certificate(common_name=common_name,
                                                                    host=host,
                                                                    days_valid=days,
                                                                    curve=ec.SECP384R1)  # TODO: use Config class?

        certificate_filepath = os.path.join(self.node_configuration.known_certificates_dir, full_filepath)
        _save_tls_certificate(certificate=certificate, full_filepath=certificate_filepath)


uses_config = click.make_pass_decorator(NucypherClickConfig, ensure=True)


@click.group()
@click.option('--version', is_flag=True)
@click.option('--verbose', is_flag=True)
@click.option('--dev', is_flag=True)
@click.option('--federated-only', is_flag=True)
@click.option('--config-root', type=click.Path())
@click.option('--config-file', type=click.Path())
@click.option('--metadata-dir', type=click.Path())
@click.option('--provider-uri', type=str)
@click.option('--compile', is_flag=True)
@click.option('--registry-filepath', type=click.Path())
@click.option('--deployer', is_flag=True)
@click.option('--poa', is_flag=True)
@uses_config
def cli(config,
        verbose,
        version,
        dev,
        federated_only,
        config_root,
        config_file,
        metadata_dir,
        provider_uri,
        registry_filepath,
        deployer,
        compile,
        poa):

    click.echo(BANNER)

    # Store config data
    config.verbose = verbose

    config.dev = dev
    config.federated_only = federated_only
    config.config_root = config_root
    config.config_file = config_file
    config.metadata_dir = metadata_dir
    config.provider_uri = provider_uri
    config.compile = compile
    config.registry_filepath = registry_filepath
    config.deployer = deployer
    config.poa = poa

    if version:
        click.echo("Version {}".format(__version__))

    if config.verbose:
        click.echo("Running in verbose mode...")

    if not config.dev:
        click.echo("WARNING: Development mode is disabled")
    else:
        click.echo("Running in development mode")


@cli.command()
@click.option('--filesystem', is_flag=True, default=False)
@click.argument('action')
@uses_config
def configure(config, action, filesystem):

    #
    # Initialize
    #
    def __initialize(configuration):
        if config.dev:
            click.echo("Using temporary storage area")
        click.confirm("Initialize new nucypher configuration?", abort=True)

        configuration.write_defaults()
        click.echo("Created configuration files at {}".format(configuration.config_root))

        if click.confirm("Do you need to generate a new wallet to use for staking?"):
            address = config.create_account()

        if click.confirm("Do you need to generate a new SSL certificate (Ursula)?"):
            certificate_filepath = os.path.join(config.node_configuration.known_certificates_dir, '{}.pem'.format(address))
            config.create_node_tls_certificate(common_name=address, full_filepath=certificate_filepath)

    def __validate():
        is_valid = True      # Until there is a reason to believe otherwise...
        try:
            if filesystem:   # Check runtime directory
                is_valid = NodeConfiguration.check_config_tree_exists(config_root=config.node_configuration.config_root)
            if config.config_file:
                is_valid = validate_configuration_file(filepath=config.node_configuration.config_file_location)
        except NodeConfiguration.InvalidConfiguration:
            is_valid = False
        finally:
            result = 'Valid' if is_valid else 'Invalid'
            click.echo('{} is {}'.format(config.node_configuration.config_root, result))

    def __destroy(configuration):
        if config.dev:
            raise NodeConfiguration.ConfigurationError("Cannot destroy a temporary node configuration")

        click.confirm("*Permanently delete* all nucypher private keys, configurations,"
                      " known nodes, certificates and files at {}?".format(configuration.config_root), abort=True)

        shutil.rmtree(configuration.config_root, ignore_errors=True)
        click.echo("Deleted configuration files at {}".format(configuration.config_root))

    #
    # Action switch
    #
    config.get_node_configuration()
    if action == "init":
        __initialize(config.node_configuration)
    elif action == "destroy":
        __destroy(config.node_configuration)
    elif action == "reset":
        __destroy(config.node_configuration)
        __initialize(config.node_configuration)
    elif action == "validate":
        __validate()


@cli.command()
@click.option('--checksum-address', help="The account to lock/unlock instead of the default", type=str)
@click.argument('action', default='list', required=False)
@uses_config
def accounts(config, action, checksum_address):
    """Manage local and hosted node accounts"""

    #
    # Initialize
    #
    config.get_node_configuration()
    if not config.federated_only:
        config.connect_to_blockchain()
        config.connect_to_contracts()

        if not checksum_address:
            checksum_address = config.blockchain.interface.w3.eth.coinbase
            click.echo("WARNING: No checksum address specified - Using the node's default account.")

        def __collect_transfer_details(denomination: str):
            destination = click.prompt("Enter destination checksum_address")
            if not is_checksum_address(destination):
                click.echo("{} is not a valid checksum checksum_address".format(destination))
                raise click.Abort()
            amount = click.prompt("Enter amount of {} to transfer".format(denomination), type=int)
            return destination, amount

    #
    # Action Switch
    #
    if action == 'new':
        new_address = config.create_account()
        click.echo("Created new ETH address {}".format(new_address))
        if click.confirm("Set new address as the node's keying default account?".format(new_address)):
            config.blockchain.interface.w3.eth.defaultAccount = new_address
            click.echo("{} is now the node's default account.".format(config.blockchain.interface.w3.eth.defaultAccount))

    if action == 'set-default':
        config.blockchain.interface.w3.eth.defaultAccount = checksum_address # TODO: is there a better way to do this?
        click.echo("{} is now the node's default account.".format(config.blockchain.interface.w3.eth.defaultAccount))

    elif action == 'export':
        keyring = NucypherKeyring(common_name=checksum_address)
        click.confirm("Export local private key for {} to node's keyring: {}?".format(checksum_address, config.provider_uri), abort=True)
        passphrase = click.prompt("Enter passphrase to decrypt account", type=str)
        keyring._export(blockchain=config.blockchain, passphrase=passphrase)

    elif action == 'list':
        for index, checksum_address in enumerate(config.accounts):
            token_balance = config.token_agent.get_balance(address=checksum_address)
            eth_balance = config.blockchain.interface.w3.eth.getBalance(checksum_address)
            base_row_template = ' {address}\n    Tokens: {tokens}\n    ETH: {eth}\n '
            row_template = ('\netherbase |'+base_row_template) if not index else '{index} ....... |'+base_row_template
            row = row_template.format(index=index, address=checksum_address, tokens=token_balance, eth=eth_balance)
            click.echo(row)

    elif action == 'balance':
        if not checksum_address:
            checksum_address = config.blockchain.interface.w3.eth.etherbase
            click.echo('No checksum_address supplied, Using the default {}'.format(checksum_address))
        token_balance = config.token_agent.get_balance(address=checksum_address)
        eth_balance = config.token_agent.blockchain.interface.w3.eth.getBalance(checksum_address)
        click.echo("Balance of {} | Tokens: {} | ETH: {}".format(checksum_address, token_balance, eth_balance))

    elif action == "transfer-tokens":
        destination, amount = __collect_transfer_details(denomination='tokens')
        click.confirm("Are you sure you want to send {} tokens to {}?".format(amount, destination), abort=True)
        txhash = config.token_agent.transfer(amount=amount, target_address=destination, sender_address=checksum_address)
        config.blockchain.wait_for_receipt(txhash)
        click.echo("Sent {} tokens to {} | {}".format(amount, destination, txhash))

    elif action == "transfer-eth":
        destination, amount = __collect_transfer_details(denomination='ETH')
        tx = {'to': destination, 'from': checksum_address, 'value': amount}
        click.confirm("Are you sure you want to send {} tokens to {}?".format(tx['value'], tx['to']), abort=True)
        txhash = config.blockchain.interface.w3.eth.sendTransaction(tx)
        config.blockchain.wait_for_receipt(txhash)
        click.echo("Sent {} ETH to {} | {}".format(amount, destination, str(txhash)))


@cli.command()
@click.option('--checksum-address', help="Send rewarded tokens to a specific address, instead of the default.", type=str)
@click.option('--value', help="Stake value in the smallest denomination", type=int)
@click.option('--duration', help="Stake duration in periods", type=int)
@click.option('--index', help="A specific stake index to resume", type=int)
@click.argument('action', default='list', required=False)
@uses_config
def stake(config, action, checksum_address, index, value, duration):
    """
    Manage active and inactive node blockchain stakes.

    Arguments
    ==========

    action - Which action to perform; The choices are:

        - list: List all stakes for this node
        - info: Display info about a specific stake
        - start: Start the staking daemon
        - confirm-activity: Manually confirm-activity for the current period
        - divide-stake: Divide an existing stake

    value - The quantity of tokens to stake.

    periods - The duration (in periods) of the stake.

    Options
    ========

    --wallet-address - A valid ethereum checksum address to use instead of the default
    --stake-index - The zero-based stake index, or stake tag for this wallet-address

    """

    #
    # Initialize
    #
    config.get_node_configuration()
    if not config.federated_only:
        config.connect_to_blockchain()
        config.connect_to_contracts()

    if not checksum_address:

        for index, address in enumerate(config.accounts):
            if index == 0:
                row = 'etherbase (0) | {}'.format(address)
            else:
                row = '{} .......... | {}'.format(index, address)
            click.echo(row)

        click.echo("Select ethereum address")
        account_selection = click.prompt("Enter 0-{}".format(len(config.accounts)), type=int)
        address = config.accounts[account_selection]

    if action == 'list':
        live_stakes = config.miner_agent.get_all_stakes(miner_address=address)
        for index, stake_info in enumerate(live_stakes):
            row = '{} | {}'.format(index, stake_info)
            click.echo(row)

    elif action == 'init':
        click.confirm("Stage a new stake?", abort=True)

        live_stakes = config.miner_agent.get_all_stakes(miner_address=address)
        if len(live_stakes) > 0:
            raise RuntimeError("There is an existing stake for {}".format(address))

        # Value
        balance = config.token_agent.get_balance(address=address)
        click.echo("Current balance: {}".format(balance))
        value = click.prompt("Enter stake value", type=int)

        # Duration
        message = "Minimum duration: {} | Maximum Duration: {}".format(constants.MIN_LOCKED_PERIODS,
                                                                       constants.MAX_REWARD_PERIODS)
        click.echo(message)
        duration = click.prompt("Enter stake duration in days", type=int)

        start_period = config.miner_agent.get_current_period()
        end_period = start_period + duration

        # Review
        click.echo("""
        
        | Staged Stake |
        
        Node: {address}
        Value: {value}
        Duration: {duration}
        Start Period: {start_period}
        End Period: {end_period}
        
        """.format(address=address,
                   value=value,
                   duration=duration,
                   start_period=start_period,
                   end_period=end_period))

        if not click.confirm("Is this correct?"):
            # field = click.prompt("Which stake field do you want to edit?")
            raise NotImplementedError

        # Initialize the staged stake
        config.miner_agent.deposit_tokens(amount=value, lock_periods=duration, sender_address=address)

        proc_params = ['run_ursula']
        processProtocol = UrsulaProcessProtocol(command=proc_params)
        ursula_proc = reactor.spawnProcess(processProtocol, "nucypher-cli", proc_params)

    elif action == 'resume':
        """Reconnect and resume an existing live stake"""

        proc_params = ['run_ursula']
        processProtocol = UrsulaProcessProtocol(command=proc_params)
        ursula_proc = reactor.spawnProcess(processProtocol, "nucypher-cli", proc_params)

    elif action == 'confirm-activity':
        """Manually confirm activity for the active period"""

        stakes = config.miner_agent.get_all_stakes(miner_address=address)
        if len(stakes) == 0:
            raise RuntimeError("There are no active stakes for {}".format(address))
        config.miner_agent.confirm_activity(node_address=address)

    elif action == 'divide':
        """Divide an existing stake by specifying the new target value and end period"""

        stakes = config.miner_agent.get_all_stakes(miner_address=address)
        if len(stakes) == 0:
            raise RuntimeError("There are no active stakes for {}".format(address))

        if not index:
            for selection_index, stake_info in enumerate(stakes):
                click.echo("{} ....... {}".format(selection_index, stake_info))
            index = click.prompt("Select a stake to divide", type=int)

        target_value = click.prompt("Enter new target value", type=int)
        extension = click.prompt("Enter number of periods to extend", type=int)

        click.echo("""
        Current Stake: {}
        
        New target value {}
        New end period: {}
        
        """.format(stakes[index],
                   target_value,
                   target_value+extension))

        click.confirm("Is this correct?", abort=True)
        config.miner_agent.divide_stake(miner_address=address,
                                        stake_index=index,
                                        value=value,
                                        periods=extension)

    elif action == 'collect-reward':
        """Withdraw staking reward to the specified wallet address"""
        # click.confirm("Send {} to {}?".format)
        # config.miner_agent.collect_staking_reward(collector_address=address)
        raise NotImplementedError

    elif action == 'abort':
        click.confirm("Are you sure you want to abort the staking process?", abort=True)
        # os.kill(pid=NotImplemented)
        raise NotImplementedError


@cli.command()
@click.option('--geth', is_flag=True)
@click.option('--pyevm', is_flag=True)
@click.option('--nodes', help="The number of nodes to simulate", type=int, default=10)
@click.argument('action')
@uses_config
def simulate(config, action, nodes, geth, pyevm):
    """
    Locally simulate the nucypher blockchain network

    action - Which action to perform; The choices are:
           - start: Start a multi-process nucypher network simulation
           - stop: Stop a running simulation gracefully


    --nodes - The quantity of nodes (processes) to execute during the simulation
    --duration = The number of periods to run the simulation before termination

    """

    if action == 'start':

        #
        # Blockchain Connection
        #
        if config.sim_processes is constants.NO_SIMULATION_RUNNING:
            config.sim_processes = list()
        elif len(config.sim_processes) != 0:
            for process in config.sim_processes:
                config.sim_processes.remove(process)
                os.kill(process.pid, 9)

        if not config.federated_only:
            if geth:
                config.provider_uri = "ipc:///tmp/geth.ipc"
            elif pyevm:
                config.provider_uri = "tester://pyevm"
            sim_provider_uri = config.provider_uri

            # Sanity check
            supported_sim_uris = ("tester://geth", "tester://pyevm", "ipc:///tmp/geth.ipc")
            if config.provider_uri not in supported_sim_uris:
                message = "{} is not a supported simulation node backend. Supported URIs are {}"
                click.echo(message.format(config.provider_uri, supported_sim_uris))
                raise click.Abort()

            simulation_registry = TemporaryEthereumContractRegistry()
            simulation_interface = BlockchainDeployerInterface(provider_uri=sim_provider_uri,
                                                               registry=simulation_registry,
                                                               compiler=SolidityCompiler())

            sim_blockchain = TesterBlockchain(interface=simulation_interface, test_accounts=nodes, airdrop=False)

            accounts = sim_blockchain.interface.w3.eth.accounts
            origin, *everyone_else = accounts

            # Set the deployer address from the freshly created test account
            simulation_interface.deployer_address = origin

            #
            # Blockchain Action
            #
            sim_blockchain.ether_airdrop(amount=DEVELOPMENT_ETH_AIRDROP_AMOUNT)

            click.confirm("Deploy all nucypher contracts to {}?".format(config.provider_uri), abort=True)
            click.echo("Bootstrapping simulated blockchain network")

            # Deploy contracts
            token_deployer = NucypherTokenDeployer(blockchain=sim_blockchain, deployer_address=origin)
            token_deployer.arm()
            token_deployer.deploy()
            sim_token_agent = token_deployer.make_agent()

            miners_escrow_secret = os.urandom(DISPATCHER_SECRET_LENGTH)
            miner_escrow_deployer = MinerEscrowDeployer(token_agent=sim_token_agent,
                                                        deployer_address=origin,
                                                        secret_hash=miners_escrow_secret)
            miner_escrow_deployer.arm()
            miner_escrow_deployer.deploy()
            sim_miner_agent = miner_escrow_deployer.make_agent()

            policy_manager_secret = os.urandom(DISPATCHER_SECRET_LENGTH)
            policy_manager_deployer = PolicyManagerDeployer(miner_agent=sim_miner_agent,
                                                            deployer_address=origin,
                                                            secret_hash=policy_manager_secret)
            policy_manager_deployer.arm()
            policy_manager_deployer.deploy()
            policy_agent = policy_manager_deployer.make_agent()

            airdrop_amount = DEVELOPMENT_TOKEN_AIRDROP_AMOUNT
            click.echo("Airdropping tokens {} to {} addresses".format(airdrop_amount, len(everyone_else)))
            _receipts = token_airdrop(token_agent=sim_token_agent,
                                      origin=origin,
                                      addresses=everyone_else,
                                      amount=airdrop_amount)

            # Commit the current state of deployment to a registry file.
            click.echo("Writing filesystem registry")
            _sim_registry_name = sim_blockchain.interface.registry.commit(filepath=DEFAULT_SIMULATION_REGISTRY_FILEPATH)

        click.echo("Ready to run swarm.")

        #
        # Swarm
        #

        # Select a port range to use on localhost for sim servers

        if not config.federated_only:
            sim_addresses = everyone_else
        else:
            sim_addresses = NotImplemented

        start_port, counter = 8787, 0
        for sim_port_number, sim_address in enumerate(sim_addresses, start=start_port):

            #
            # Parse sim-ursula parameters
            #

            sim_db_name = 'sim-{}'.format(sim_port_number)

            process_params = ['nucypher-cli', '--dev']
            if geth is True:
                process_params.append('--poa')
            if config.federated_only:
                process_params.append('--federated-only')
            else:
                process_params.extend('--registry-filepath {} --provider-uri {}'.format(simulation_registry.filepath,
                                                                                        sim_provider_uri).split())
            ursula_params = '''ursula run --rest-port {} --db-name {}'''.format(sim_port_number, sim_db_name).split()
            process_params.extend(ursula_params)

            if not config.federated_only:
                min_stake, balance = MIN_ALLOWED_LOCKED, DEVELOPMENT_TOKEN_AIRDROP_AMOUNT
                value = random.randint(min_stake, balance)                            # stake a random amount...
                min_locktime, max_locktime = MIN_LOCKED_PERIODS, MAX_MINTING_PERIODS  # ...for a random lock duration
                periods = random.randint(min_locktime, max_locktime)
                process_params.extend('--checksum-address {}'.format(sim_address).split())
                process_params.extend('--stake-amount {} --stake-periods {}'.format(value, periods).split())

            # Spawn
            click.echo("Spawning node #{}".format(counter+1))
            processProtocol = UrsulaProcessProtocol(command=process_params, checksum_address=sim_address)
            cli_exec = os.path.join(BASE_DIR, 'cli', 'main.py')
            ursula_process = reactor.spawnProcess(processProtocol=processProtocol,
                                                  executable=cli_exec,
                                                  args=process_params,
                                                  env=os.environ)

            config.sim_processes.append(ursula_process)

            #
            # post-spawnProcess
            #

            # Start with some basic status data, then build on it

            rest_uri = "https://{}:{}".format('localhost', sim_port_number)

            sim_data = "prepared simulated Ursula | ReST {}".format(rest_uri)
            rest_uri = "{host}:{port}".format(host='localhost', port=str(sim_port_number))
            sim_data.format(rest_uri)

            if not config.federated_only:
                sim_data += '| ETH address {}'.format(sim_address)

            click.echo(sim_data)
            counter += 1

        click.echo("Starting the reactor")
        click.confirm("Start the reactor?", abort=True)
        try:
            reactor.run()
        finally:
            if not config.federated_only:
                click.echo("Removing simulation registry")
                os.remove(DEFAULT_SIMULATION_REGISTRY_FILEPATH)
            click.echo("Stopping simulated Ursula processes")
            for process in config.sim_processes:
                os.kill(process.pid, 9)
                click.echo("Killed {}".format(process))
            click.echo("Simulation Stopped")

    elif action == 'stop':
        # Kill the simulated ursulas
        for process in config.ursula_processes:
            process.transport.signalProcess('KILL')

    elif action == 'status':
        if not config.simulation_running:
            status_message = "Simulation not running."
        else:
            ursula_processes = len(config.ursula_processes)
            status_message = """
            
            | Node Swarm Simulation Status |
            
            Simulation processes .............. {}
            
            """.format(ursula_processes)
        click.echo(status_message)


@cli.command()
@click.option('--contract-name', type=str)
@click.option('--force', is_flag=True)
@click.option('--deployer_address', type=str)
@click.argument('action')
@uses_config
def deploy(config, action, deployer_address, contract_name, force):
    if not config.deployer:
        click.echo("The --deployer flag must be used to issue the deploy command.")
        raise click.Abort()

    def __get_deployers():

        config.connect_to_blockchain()
        config.blockchain.interface.deployer_address = deployer_address or config.accounts[0]

        DeployerInfo = collections.namedtuple('DeployerInfo', 'deployer_class upgradeable agent_name dependant')
        deployers = collections.OrderedDict({

            NucypherTokenDeployer._contract_name: DeployerInfo(deployer_class=NucypherTokenDeployer,
                                                               upgradeable=False,
                                                               agent_name='token_agent',
                                                               dependant=None),

            MinerEscrowDeployer._contract_name: DeployerInfo(deployer_class=MinerEscrowDeployer,
                                                             upgradeable=True,
                                                             agent_name='miner_agent',
                                                             dependant='token_agent'),

            PolicyManagerDeployer._contract_name: DeployerInfo(deployer_class=PolicyManagerDeployer,
                                                               upgradeable=True,
                                                               agent_name='policy_agent',
                                                               dependant='miner_agent'
                                                               ),

            # UserEscrowDeployer._contract_name: DeployerInfo(deployer_class=UserEscrowDeployer,
            #                                                 upgradeable=True,
            #                                                 agent_name='user_agent',
            #                                                 dependant='policy_agent'),  # TODO
        })

        click.confirm("Continue?", abort=True)
        return deployers

    if action == "contracts":
        deployers = __get_deployers()
        __deployment_transactions = dict()
        __deployment_agents = dict()

        available_deployers = ", ".join(deployers)
        click.echo("\n-----------------------------------------------")
        click.echo("Available Deployers: {}".format(available_deployers))
        click.echo("Blockchain Provider URI ... {}".format(config.blockchain.interface.provider_uri))
        click.echo("Registry Output Filepath .. {}".format(config.blockchain.interface.registry.filepath))
        click.echo("Deployer's Address ........ {}".format(config.blockchain.interface.deployer_address))
        click.echo("-----------------------------------------------\n")

        def __deploy_contract(deployer_class: ClassVar,
                              upgradeable: bool,
                              agent_name: str,
                              dependant: str = None
                              ) -> Tuple[dict, EthereumContractAgent]:

            __contract_name = deployer_class._contract_name

            __deployer_init_args = dict(blockchain=config.blockchain,
                                        deployer_address=config.blockchain.interface.deployer_address)

            if dependant is not None:
                __deployer_init_args.update({dependant: __deployment_agents[dependant]})

            if upgradeable:
                def __collect_secret():
                    # secret = click.prompt("Enter secret hash for {}".format(__contract_name))
                    # secret_confirmation = click.prompt("Confirm secret hash for {}".format(__contract_name))
                    secret = os.urandom(32)  # TODO: How to we handle deployment secrets?
                    secret_confirmation = secret[:]

                    if len(bytes(secret)) != 32:
                        click.echo("Deployer secret must be 32 bytes.")
                        if click.prompt("Try again?"):
                            return __collect_secret()
                    if secret != secret_confirmation:
                        click.echo("Secrets did not match")
                        if click.prompt("Try again?"):
                            return __collect_secret()
                        else:
                            raise click.Abort()
                    __deployer_init_args.update({'secret_hash': secret})
                    return secret
                __collect_secret()

            __deployer = deployer_class(**__deployer_init_args)

            #
            # Arm
            #
            if not force:
                click.confirm("Arm {}?".format(deployer_class.__name__), abort=True)

            is_armed, disqualifications = __deployer.arm(abort=False)
            if not is_armed:
                disqualifications = ', '.join(disqualifications)
                click.echo("Failed to arm {}. Disqualifications: {}".format(__contract_name, disqualifications))
                raise click.Abort()

            #
            # Deploy
            #
            if not force:
                click.confirm("Deploy {}?".format(__contract_name), abort=True)
            __transactions = __deployer.deploy()
            __deployment_transactions[__contract_name] = __transactions

            __agent = __deployer.make_agent()
            __deployment_agents[agent_name] = __agent

            return __transactions, __agent

        if contract_name:
            #
            # Deploy Single Contract
            #
            try:
                deployer_info = deployers[contract_name]
            except KeyError:
                click.echo("No such contract {}. Available contracts are {}".format(contract_name, available_deployers))
            else:
                _txs, _agent = __deploy_contract(deployer_info.deployer_class,
                                                 upgradeable=deployer_info.upgradeable,
                                                 agent_name=deployer_info.agent_name,
                                                 dependant=deployer_info.dependant)
        else:
            #
            # Deploy All Contracts
            #
            for deployer_name, deployer_info in deployers.items():
                _txs, _agent = __deploy_contract(deployer_info.deployer_class,
                                                 upgradeable=deployer_info.upgradeable,
                                                 agent_name=deployer_info.agent_name,
                                                 dependant=deployer_info.dependant)

        if not force and click.prompt("View deployment transaction hashes?"):
            for contract_name, transactions in __deployment_transactions.items():
                click.echo(contract_name)
                for tx_name, txhash in transactions.items():
                    click.echo("{}:{}".format(tx_name, txhash))

        if not force and click.confirm("Save transaction hashes to JSON file?"):
            filepath = click.prompt("Enter output filepath", type=click.Path())
            with open(filepath, 'w') as file:
                file.write(json.dumps(__deployment_transactions))
            click.echo("Successfully wrote transaction hashes file to {}".format(filepath))


@cli.command()
@click.option('--contracts', help="Echo nucypher smart contract info", is_flag=True)
@click.option('--network', help="Echo the network status", is_flag=True)
@uses_config
def status(config, contracts, network):
    """
    Echo a snapshot of live network metadata.
    """
    #
    # Initialize
    #
    config.get_node_configuration()
    if not config.federated_only:
        config.connect_to_blockchain()
        config.connect_to_contracts()

    contract_payload = """
    
    | NuCypher ETH Contracts |
    
    Provider URI ............. {provider_uri}
    Registry Path ............ {registry_filepath}

    NucypherToken ............ {token}
    MinerEscrow .............. {escrow}
    PolicyManager ............ {manager}
        
    """.format(provider_uri=config.blockchain.interface.provider_uri,
               registry_filepath=config.blockchain.interface.registry.filepath,
               token=config.token_agent.contract_address,
               escrow=config.miner_agent.contract_address,
               manager=config.policy_agent.contract_address,
               period=config.miner_agent.get_current_period())

    network_payload = """
    | Blockchain Network |
    
    Current Period ........... {period}
    Gas Price ................ {gas_price}
    Active Staking Ursulas ... {ursulas}
    
    """.format(period=config.miner_agent.get_current_period(),
               gas_price=config.blockchain.interface.w3.eth.gasPrice,
               ursulas=config.miner_agent.get_miner_population())

    subpayloads = ((contracts, contract_payload),
                   (network, network_payload),
                   )

    if not any(sp[0] for sp in subpayloads):
        payload = ''.join(sp[1] for sp in subpayloads)
    else:
        payload = str()
        for requested, subpayload in subpayloads:
            if requested is True:
                payload += subpayload

    click.echo(payload)


@cli.command()
@click.option('--rest-host', type=str)
@click.option('--rest-port', type=int)
@click.option('--db-name', type=str)
@click.option('--checksum-address', type=str)
@click.option('--stake-amount', type=int)
@click.option('--stake-periods', type=int)
@click.option('--resume', is_flag=True)
@click.argument('action')
@uses_config
def ursula(config,
           action,
           rest_port,
           rest_host,
           db_name,
           checksum_address,
           stake_amount,
           stake_periods,
           resume  # TODO Implement stake resume
           ) -> None:
    """
    Manage and Run Ursula Nodes.

    Here is the procedure to "spin-up" an Ursula node.

        0. Validate CLI Input
        1. Initialize UrsulaConfiguration (from configuration file or inline)
        2. Initialize Ursula with Passphrase
        3. Initialize Staking Loop
        4. Run TLS deployment (Learning Loop + Reactor)

    """
    if action == 'run':                # 0
        if not config.federated_only:
            if not all((stake_amount, stake_periods)) and not resume:
                click.echo("Either --stake-amount and --stake-periods options "
                           "or the --resume flag is required to run a non-federated Ursula")
                raise click.Abort()

        if config.config_file:         # 1
            ursula_config = UrsulaConfiguration.from_configuration_file(filepath=config.config_file)
        else:
            ursula_config = UrsulaConfiguration(temp=config.dev,
                                                auto_initialize=config.dev,
                                                poa=config.poa,
                                                rest_host=rest_host,
                                                rest_port=rest_port,
                                                db_name=db_name,
                                                is_me=True,
                                                federated_only=config.federated_only,
                                                registry_filepath=config.registry_filepath,
                                                provider_uri=config.provider_uri,
                                                checksum_address=checksum_address,
                                                save_metadata=False,
                                                load_metadata=True,
                                                known_metadata_dir=config.metadata_dir,
                                                start_learning_now=True,
                                                abort_on_learning_error=config.dev)

        try:
            passphrase = click.prompt("Enter passphrase to unlock account", type=str)
            URSULA = ursula_config.produce(passphrase=passphrase)  # 2
            if not config.federated_only:
                URSULA.stake(amount=stake_amount,                  # 3
                             lock_periods=stake_periods)
            URSULA.get_deployer().run()                            # 4

        finally:
            click.echo("Cleaning up.")
            ursula_config.cleanup()
            click.echo("Exited gracefully.")


if __name__ == "__main__":
    cli()

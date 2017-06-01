from input_generation import BaseInputGenerator
from test_validation import TestValidator
import utils
import os
import logging
import re

bin_dir = os.path.abspath('./crest/bin')
lib_dir = os.path.abspath('./crest/lib')
include_dir = os.path.abspath('./crest/include')
name = 'crest'
test_name_pattern = re.compile('input[0-9]+')


def get_test_files(exclude=[]):
    all_tests = [t for t in os.listdir('.') if test_name_pattern.match(utils.get_file_name(t))]
    return [t for t in all_tests if utils.get_file_name(t) not in exclude]


class InputGenerator(BaseInputGenerator):

    def __init__(self, timelimit=None, log_verbose=False, search_heuristic='cfg', machine_model='32bit'):
        super().__init__(timelimit, machine_model)
        self.log_verbose = log_verbose

        self._run_env = utils.get_env_with_path_added(bin_dir)

        self.search_heuristic = search_heuristic
        self.num_iterations = timelimit if timelimit else 1500

    def get_name(self):
        return name

    def get_run_env(self):
        return self._run_env

    def get_test_count(self):
        return len(get_test_files())

    def prepare(self, filecontent):
        content = '#include<crest.h>\n'
        content += filecontent
        content += '\n'
        nondet_methods_used = utils.get_nondet_methods(filecontent)
        for method in nondet_methods_used:  # append method definition at end of file content
            nondet_method_definition = self._get_nondet_method(method)
            content += nondet_method_definition
        return content

    def _get_nondet_method(self, method_name):
        m_type = utils.get_return_type(method_name)
        return self._create_nondet_method(method_name, m_type)

    def is_supported_type(self, method_type):
        return method_type in ['int', 'short', 'char', 'unsigned int', 'unsigned short', 'unsigned char']

    def _create_nondet_method(self, method_name, method_type):
        if not self.is_supported_type(method_type):
            logging.error('Crest can\'t handle symbolic values of type ' + method_type)
            internal_type = 'unsigned int'
            logging.warning('Continuing with type %s for method %s', internal_type, method_name)
        else:
            internal_type = method_type
        var_name = '__sym_' + method_name[len('__VERIFIER_nondet_'):]
        marker_method_call = 'CREST_' + '_'.join(internal_type.split())
        method = '{0} {1}() {{\n    {4} {2};\n    {3}({2});\n'.\
            format(method_type, method_name, var_name, marker_method_call, internal_type)
        if method_type == internal_type:
            method += '    return {0};\n'.format(var_name)
        else:
            method += '    return ({0}) {1};\n'.format(method_type, var_name)
        method += '}\n'

        return method

    def create_input_generation_cmds(self, filename):
        compile_cmd = [os.path.join(bin_dir, 'crestc'),
                       filename]
        # the output file name created by crestc is 'input file name - '.c'
        instrumented_file = filename[:-2]
        input_gen_cmd = [os.path.join(bin_dir, 'run_crest'),
                         instrumented_file,
                         str(self.num_iterations),
                         '-' + self.search_heuristic]
        return [compile_cmd, input_gen_cmd]


class CrestTestValidator(TestValidator):

    def __init__(self, machine_model='32bit'):
        super().__init__(machine_model)
        self.counter = 0

    def get_name(self):
        return name

    def get_test_vector(self, test):
        test_vector = dict()
        with open(test, 'r') as inp:
            for line in inp.readlines():
                try:
                    test_vector[str(self.counter)] = {'name': None, 'value': line.strip()}
                    self.counter += 1
                except ValueError as e:
                    raise AssertionError(e)
        if not test_vector:
            return None
        else:
            return test_vector

    def create_witness(self, filename, test_file, test_vector):
        """
        Creates a witness for the test file produced by crest.
        Test files produced by our version of crest specify one test value per line, without
        any mention of the variable the value is assigned to.
        Because of this, we have to build a fancy witness automaton of the following format:
        For each test value specified in the test file, there is one precessor and one
        successor state. These two states are connected by one transition for each
        call to a CREST_x(..) function. Each of these transitions has the assumption,
        that the variable specified in the corresponding CREST_x(..) function has the current
        test value.
        """
        witness = self.witness_creator.create_witness(producer=self.get_name(),
                                                      filename=filename,
                                                      test_vector=test_vector,
                                                      nondet_methods=utils.get_nondet_methods(filename),
                                                      machine_model=self.machine_model,
                                                      error_line=self.get_error_line(filename))

        test_name = os.path.basename(test_file)
        witness_file = test_name + ".witness.graphml"
        witness_file = utils.get_file_path(witness_file, temp_dir=False)

        return {'name': witness_file, 'content': witness}

    def create_harness(self, filename, test_file, test_vector):
        # If no inputs are defined don't create a witness
        harness = self.harness_creator.create_harness(producer=self.get_name(),
                                                      filename=filename,
                                                      test_vector=test_vector,
                                                      nondet_methods=utils.get_nondet_methods(filename),
                                                      error_method=utils.error_method)
        test_name = os.path.basename(test_file)
        harness_file = test_name + '.harness.c'
        harness_file = utils.get_file_path(harness_file, temp_dir=False)

        return {'name': harness_file, 'content': harness}

    def _create_all_x(self, filename, creation_method, visited_tests):
        created_content = []
        new_test_files = get_test_files(visited_tests)
        if len(new_test_files) > 0:
            logging.info("Looking at %s test file(s)", len(new_test_files))
        empty_case_handled = False
        for test_file in new_test_files:
            logging.debug("Looking at test case %s", test_file)
            test_name = utils.get_file_name(test_file)
            assert test_name not in visited_tests
            assert os.path.exists(test_file)
            visited_tests.add(test_name)
            test_vector = self.get_test_vector(test_file)
            if test_vector or not empty_case_handled:
                if not test_vector:
                    test_vector = dict()
                new_content = creation_method(filename, test_file, test_vector)
                if new_content:  # It's possible that no witness is created due to a missing test vector
                    created_content.append(new_content)
            else:
                logging.debug("Test vector was not generated for %s", test_file)
        return created_content

    def create_all_witnesses(self, filename, visited_tests):
        return self._create_all_x(filename, self.create_witness, visited_tests)

    def create_all_harnesses(self, filename, visited_tests):
        return self._create_all_x(filename, self.create_harness, visited_tests)

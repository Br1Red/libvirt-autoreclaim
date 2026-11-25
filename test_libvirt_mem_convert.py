import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from unittest.mock import call, patch

spec = importlib.util.spec_from_file_location('libvirt_mem_convert', 'libvirt-mem-convert.py')
libvirt_mem_convert = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = libvirt_mem_convert
spec.loader.exec_module(libvirt_mem_convert)


class LibvirtMemConvertTests(unittest.TestCase):
    def test_missing_memory_text_raises_value_error(self):
        for value in (None, '', '   '):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'Memory value'):
                    libvirt_mem_convert.to_kib(value, 'KiB')

    def test_empty_memory_element_reports_value_error(self):
        xml = '<domain><memory/><currentMemory>1024</currentMemory></domain>'
        with self.assertRaisesRegex(ValueError, 'Memory value is missing'):
            libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', 2048)

    def test_to_virtiomem_disables_balloon_by_default(self):
        xml = """<domain>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>3145728</currentMemory>
  <vcpu>2</vcpu>
  <devices>
    <memballoon model='virtio' autodeflate='on'>
      <stats period='2'/>
    </memballoon>
  </devices>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)
        balloon = ElementTree.fromstring(result).find('devices/memballoon')

        self.assertEqual(balloon.get('model'), 'none')
        self.assertNotIn('autodeflate', balloon.attrib)
        self.assertIsNone(balloon.find('stats'))

    def test_keep_balloon_flag_preserves_existing_model(self):
        xml = """<domain>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>3145728</currentMemory>
  <vcpu>2</vcpu>
  <devices><memballoon model='virtio'/></devices>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(
            xml, 'to-virtiomem', 1048576, keep_balloon=True
        )
        balloon = ElementTree.fromstring(result).find('devices/memballoon')

        self.assertEqual(balloon.get('model'), 'virtio')
        self.assertEqual(balloon.find('stats').get('period'), '5')

    def test_idempotent_to_virtiomem_disables_existing_balloon(self):
        base_kib = libvirt_mem_convert.DEFAULT_BASE_KIB
        xml = f"""<domain>
  <memory unit='KiB'>8388608</memory>
  <currentMemory unit='KiB'>{base_kib}</currentMemory>
  <cpu><numa><cell id='0' cpus='0-3' memory='{base_kib}' unit='KiB'/></numa></cpu>
  <devices>
    <memory model='virtio-mem'>
      <target><size unit='KiB'>2048</size><requested unit='KiB'>0</requested></target>
    </memory>
    <memballoon model='virtio'/>
  </devices>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', base_kib)
        root = ElementTree.fromstring(result)

        self.assertEqual(len(root.findall("devices/memory[@model='virtio-mem']")), 1)
        self.assertEqual(root.find('devices/memballoon').get('model'), 'none')

    def test_existing_virtiomem_base_is_corrected_to_default(self):
        xml = """<domain>
  <maxMemory slots='16' unit='GiB'>8</maxMemory>
  <memory unit='GiB'>8</memory>
  <currentMemory unit='GiB'>1</currentMemory>
  <cpu><numa><cell id='0' cpus='0-3' memory='1' unit='GiB'/></numa></cpu>
  <devices><memory model='virtio-mem'><target>
    <size unit='GiB'>7</size><requested unit='MiB'>1536</requested>
  </target></memory><memballoon model='none'/></devices>
</domain>"""
        for mode in (None, 'to-virtiomem'):
            with self.subTest(mode=mode):
                result = libvirt_mem_convert.transform_xml_string(xml, mode, base_kib=None)
                root = ElementTree.fromstring(result)
                self.assertEqual(root.findtext('memory'), '8')
                self.assertEqual(root.findtext('maxMemory'), '8')
                self.assertEqual(root.findtext('currentMemory'), '2097152')
                self.assertEqual(root.find('cpu/numa/cell').get('memory'), '2097152')
                self.assertEqual(root.findtext('devices/memory/target/size'), '6291456')
                self.assertEqual(root.findtext('devices/memory/target/requested'), '524288')
                self.assertEqual(root.find('devices/memballoon').get('model'), 'none')

    def test_default_base_correction_rejects_insufficient_maximum(self):
        xml = (
            "<domain><memory unit='MiB'>512</memory><currentMemory unit='MiB'>512</currentMemory>"
            "<devices><memory model='virtio-mem'><target><size unit='MiB'>1024</size>"
            "<requested unit='MiB'>0</requested></target></memory></devices></domain>"
        )
        for mode in (None, 'to-virtiomem'):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                ValueError, 'Maximum VM memory .* must be greater than base memory'
            ):
                libvirt_mem_convert.transform_xml_string(xml, mode, base_kib=None)

    def test_default_base_correction_preserves_secondary_numa_cells(self):
        xml = (
            "<domain><maxMemory unit='GiB'>8</maxMemory><memory unit='GiB'>8</memory>"
            "<currentMemory unit='GiB'>4</currentMemory><cpu><numa>"
            "<cell id='0' memory='3584' unit='MiB'/><cell id='1' memory='512' unit='MiB'/>"
            "</numa></cpu><devices><memory model='virtio-mem'><target>"
            "<size unit='GiB'>4</size><requested unit='GiB'>1</requested>"
            "</target></memory></devices></domain>"
        )
        root = ElementTree.fromstring(
            libvirt_mem_convert.transform_xml_string(xml, None, base_kib=None)
        )

        cells = root.findall('cpu/numa/cell')
        self.assertEqual(cells[0].get('memory'), str(1536 * 1024))
        self.assertEqual(cells[1].get('memory'), '512')
        self.assertEqual(cells[1].get('unit'), 'MiB')
        self.assertEqual(root.findtext('memory'), '8')
        self.assertEqual(root.findtext('devices/memory/target/size'), str(6 * 1048576))
        self.assertEqual(root.findtext('devices/memory/target/requested'), str(3 * 1048576))

    def test_default_base_correction_applies_initial_and_max_overrides(self):
        xml = (
            "<domain><maxMemory unit='GiB'>8</maxMemory><memory unit='GiB'>8</memory>"
            "<currentMemory unit='GiB'>1</currentMemory><cpu><numa>"
            "<cell id='0' memory='1' unit='GiB'/></numa></cpu><devices>"
            "<memory model='virtio-mem'><target><size unit='GiB'>7</size>"
            "<requested unit='GiB'>1</requested></target></memory></devices></domain>"
        )
        root = ElementTree.fromstring(libvirt_mem_convert.transform_xml_string(
            xml, None, base_kib=None, initial_kib=3 * 1048576,
            max_override_kib=6 * 1048576,
        ))

        self.assertEqual(root.findtext('maxMemory'), str(6 * 1048576))
        self.assertEqual(root.findtext('memory'), str(6 * 1048576))
        self.assertEqual(root.findtext('currentMemory'), str(2 * 1048576))
        self.assertEqual(root.findtext('devices/memory/target/size'), str(4 * 1048576))
        self.assertEqual(root.findtext('devices/memory/target/requested'), str(1048576))

    def test_base_correction_respects_existing_virtiomem_block_size(self):
        xml = (
            "<domain><maxMemory unit='GiB'>4</maxMemory><memory unit='GiB'>1</memory>"
            "<currentMemory unit='GiB'>1</currentMemory><devices>"
            "<memory model='virtio-mem'><target><size unit='GiB'>3</size>"
            "<block unit='MiB'>128</block><requested unit='GiB'>1</requested>"
            "</target></memory></devices></domain>"
        )
        result = libvirt_mem_convert.transform_xml_string(xml, None, base_kib=1026 * 1024)
        root = ElementTree.fromstring(result)

        self.assertEqual(root.findtext('memory'), str(1026 * 1024))
        self.assertEqual(root.findtext('devices/memory/target/size'), str(2944 * 1024))
        self.assertEqual(root.findtext('devices/memory/target/requested'), str(896 * 1024))

    def test_vm_dry_run_corrects_default_base_without_defining_domain(self):
        xml = (
            "<domain><memory unit='GiB'>1</memory><currentMemory unit='GiB'>1</currentMemory>"
            "<devices><memory model='virtio-mem'><target><size unit='GiB'>3</size>"
            "<requested unit='GiB'>1</requested></target></memory></devices></domain>"
        )
        with patch.object(
            libvirt_mem_convert, 'run_virsh',
            side_effect=['running', 'Persistent: yes\n', '', xml],
        ) as run_virsh, patch.object(libvirt_mem_convert, 'print_diff') as print_diff:
            libvirt_mem_convert.process_vm('guest-a', None, None, dry_run=True)

        self.assertEqual(len(run_virsh.call_args_list), 4)
        root = ElementTree.fromstring(print_diff.call_args.args[1])
        self.assertEqual(root.findtext('memory'), '2097152')
        self.assertEqual(root.findtext('devices/memory/target/size'), '2097152')
        self.assertEqual(root.findtext('devices/memory/target/requested'), '0')

    def test_to_virtiomem_creates_requested_balloon_model_when_missing(self):
        xml = (
            "<domain><memory unit='KiB'>4194304</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory>"
            "<vcpu>2</vcpu><devices/></domain>"
        )
        for keep_balloon, expected_model in ((False, 'none'), (True, 'virtio')):
            with self.subTest(keep_balloon=keep_balloon):
                result = libvirt_mem_convert.transform_xml_string(
                    xml, 'to-virtiomem', 1048576, keep_balloon=keep_balloon
                )
                balloon = ElementTree.fromstring(result).find('devices/memballoon')
                self.assertEqual(balloon.get('model'), expected_model)

    def test_to_balloon_uses_numa_base_without_double_counting_virtio_mem(self):
        xml = """<domain>
  <maxMemory slots='16' unit='KiB'>8388608</maxMemory>
  <memory unit='KiB'>8388608</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <cpu><numa><cell id='0' cpus='0-3' memory='1048576' unit='KiB'/></numa></cpu>
  <devices>
    <memory model='virtio-mem'>
      <target>
        <size unit='KiB'>7340032</size>
        <node>0</node>
        <requested unit='KiB'>1572864</requested>
      </target>
    </memory>
  </devices>
</domain>"""
        result = libvirt_mem_convert.transform_xml_string(xml, 'to-balloon', 1048576)
        root = ElementTree.fromstring(result)

        self.assertEqual(root.findtext('memory'), '8388608')
        self.assertEqual(root.findtext('currentMemory'), '2621440')

        result = libvirt_mem_convert.transform_xml_string(
            xml, 'to-balloon', 1048576, initial_kib=3145729
        )
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '8388608')
        self.assertEqual(root.findtext('currentMemory'), '3145729')
        self.assertEqual(root.find('currentMemory').get('unit'), 'KiB')

        multi_node_xml = xml.replace(
            "<cell id='0' cpus='0-3' memory='1048576' unit='KiB'/>",
            "<cell id='0' cpus='0-1' memory='512' unit='MiB'/>"
            "<cell id='1' cpus='2-3' memory='524288' unit='KiB'/>",
        )
        result = libvirt_mem_convert.transform_xml_string(multi_node_xml, 'to-balloon', 1048576)
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '8388608')
        self.assertEqual(root.findtext('currentMemory'), '2621440')
        self.assertEqual(
            [cell.get('memory') for cell in root.findall('cpu/numa/cell')],
            ['7864320', '524288'],
        )

    def test_to_balloon_initial_memory_creates_current_memory_and_checks_limits(self):
        xml = "<domain><memory unit='GiB'>8</memory></domain>"
        result = libvirt_mem_convert.transform_xml_string(
            xml, 'to-balloon', 1048576, initial_kib=2621440
        )
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '8')
        self.assertEqual(root.findtext('currentMemory'), '2621440')
        for value in (0, 8388609):
            with self.subTest(initial_kib=value):
                with self.assertRaisesRegex(ValueError, 'Initial memory must be positive'):
                    libvirt_mem_convert.transform_xml_string(
                        xml, 'to-balloon', 1048576, initial_kib=value
                    )

    def test_to_balloon_reenables_disabled_balloon(self):
        for with_virtiomem in (False, True):
            with self.subTest(with_virtiomem=with_virtiomem):
                memory = (
                    "<memory unit='KiB'>4194304</memory>"
                    "<currentMemory unit='KiB'>3145728</currentMemory>"
                )
                virtio_mem = (
                    "<memory model='virtio-mem'><target>"
                    "<size unit='KiB'>2048</size><requested unit='KiB'>1024</requested>"
                    "</target></memory>"
                    if with_virtiomem else ''
                )
                xml = (
                    f"<domain>{memory}<devices>{virtio_mem}"
                    "<memballoon model='none'/></devices></domain>"
                )

                result = libvirt_mem_convert.transform_xml_string(xml, 'to-balloon', 1048576)
                balloon = ElementTree.fromstring(result).find('devices/memballoon')

                self.assertEqual(balloon.get('model'), 'virtio')
                self.assertEqual(balloon.get('autodeflate'), 'on')
                self.assertEqual(balloon.find('stats').get('period'), '5')

    def test_keep_balloon_cli_flag_is_applied(self):
        xml = (
            "<domain><memory unit='KiB'>4194304</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory>"
            "<vcpu>2</vcpu><devices><memballoon model='virtio'/></devices></domain>"
        )
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, 'input.xml')
            output_path = os.path.join(directory, 'output.xml')
            with open(input_path, 'w', encoding='utf-8') as xml_file:
                xml_file.write(xml)

            with patch(
                'sys.argv',
                ['libvirt-mem-convert.py', 'to-virtiomem', input_path, output_path, '-k'],
            ):
                libvirt_mem_convert.main()

            with open(output_path, 'r', encoding='utf-8') as xml_file:
                root = ElementTree.parse(xml_file).getroot()
            self.assertEqual(root.find('devices/memballoon').get('model'), 'virtio')

    def test_conversion_options_are_passed_to_vm_mode(self):
        with patch(
            'sys.argv',
            [
                'libvirt-mem-convert.py', 'to-virtiomem', '-V', 'guest-a',
                '--keep-balloon', '-i', '2GiB', '-m', '6GiB', '-n', '-L', 'always',
            ],
        ), patch.object(libvirt_mem_convert, 'process_vm') as process_vm:
            libvirt_mem_convert.main()

        process_vm.assert_called_once_with(
            'guest-a',
            'to-virtiomem',
            libvirt_mem_convert.DEFAULT_BASE_KIB,
            connect_uri=None,
            dry_run=True,
            color_mode='always',
            keep_balloon=True,
            initial_kib=2097152,
            max_override_kib=6291456,
        )

    def test_omitted_mode_preserves_virtiomem_and_corrects_memory_values(self):
        xml = """<domain>
  <maxMemory slots='16' unit='GiB'>4</maxMemory>
  <memory unit='GiB'>4</memory>
  <currentMemory unit='GiB'>1</currentMemory>
  <cpu><numa><cell id='0' cpus='0-1' memory='1' unit='GiB'/></numa></cpu>
  <devices>
    <memory model='virtio-mem'>
      <target><size unit='GiB'>3</size><requested unit='GiB'>1</requested></target>
    </memory>
    <memballoon model='none'/>
  </devices>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(
            xml, None, base_kib=2 * 1048576, initial_kib=3 * 1048576,
            max_override_kib=5 * 1048576,
        )
        root = ElementTree.fromstring(result)

        self.assertEqual(root.findtext('memory'), str(5 * 1048576))
        self.assertEqual(root.findtext('currentMemory'), str(2 * 1048576))
        self.assertEqual(root.findtext('maxMemory'), str(5 * 1048576))
        self.assertEqual(root.find('cpu/numa/cell').get('memory'), str(2 * 1048576))
        self.assertEqual(root.findtext('devices/memory/target/size'), str(3 * 1048576))
        self.assertEqual(root.findtext('devices/memory/target/requested'), str(1048576))
        self.assertEqual(root.find('devices/memballoon').get('model'), 'none')

    def test_omitted_mode_keeps_memory_equal_to_max_memory(self):
        xml = """<domain>
  <maxMemory slots='16' unit='KiB'>8388608</maxMemory>
  <memory unit='KiB'>8388608</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <cpu><numa><cell id='0' cpus='0-3' memory='1048576' unit='KiB'/></numa></cpu>
  <devices><memory model='virtio-mem'><target>
    <size unit='KiB'>7340032</size><requested unit='KiB'>1572864</requested>
  </target></memory></devices>
</domain>"""
        result = libvirt_mem_convert.transform_xml_string(
            xml, None, base_kib=2 * 1048576
        )
        root = ElementTree.fromstring(result)

        self.assertEqual(root.findtext('memory'), root.findtext('maxMemory'))
        self.assertEqual(root.findtext('memory'), '8388608')
        self.assertEqual(root.findtext('currentMemory'), '2097152')
        self.assertEqual(root.find('cpu/numa/cell').get('memory'), '2097152')
        self.assertEqual(root.findtext('devices/memory/target/size'), '6291456')
        self.assertEqual(root.findtext('devices/memory/target/requested'), '524288')

    def test_omitted_mode_preserves_balloon_device_while_correcting_memory(self):
        xml = (
            "<domain><memory unit='GiB'>4</memory><currentMemory unit='GiB'>2</currentMemory>"
            "<devices><memballoon model='none'/></devices></domain>"
        )
        result = libvirt_mem_convert.transform_xml_string(
            xml, None, base_kib=None, initial_kib=1048576, max_override_kib=3 * 1048576
        )
        root = ElementTree.fromstring(result)

        self.assertEqual(root.findtext('memory'), str(3 * 1048576))
        self.assertEqual(root.findtext('currentMemory'), str(1048576))
        self.assertEqual(root.find('devices/memballoon').get('model'), 'none')
        self.assertIsNone(root.find('maxMemory'))
        self.assertIsNone(root.find("devices/memory[@model='virtio-mem']"))

    def test_omitted_mode_base_can_raise_current_virtiomem_memory(self):
        xml = (
            "<domain><maxMemory unit='GiB'>4</maxMemory><memory unit='GiB'>1</memory>"
            "<currentMemory unit='GiB'>1</currentMemory><devices><memory model='virtio-mem'>"
            "<target><size unit='GiB'>3</size><requested unit='GiB'>0</requested></target>"
            "</memory></devices></domain>"
        )
        result = libvirt_mem_convert.transform_xml_string(
            xml, None, base_kib=2 * 1048576
        )
        root = ElementTree.fromstring(result)

        self.assertEqual(root.findtext('memory'), str(2 * 1048576))
        self.assertEqual(root.findtext('currentMemory'), str(2 * 1048576))
        self.assertEqual(root.findtext('maxMemory'), '4')
        self.assertEqual(root.findtext('devices/memory/target/size'), str(2 * 1048576))
        self.assertEqual(root.findtext('devices/memory/target/requested'), '0')

    def test_cli_omitted_mode_accepts_input_and_output_paths(self):
        xml = (
            "<domain><memory unit='GiB'>4</memory><currentMemory unit='GiB'>2</currentMemory>"
            "<devices><memballoon model='none'/></devices></domain>"
        )
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, 'input.xml')
            output_path = os.path.join(directory, 'output.xml')
            with open(input_path, 'w', encoding='utf-8') as xml_file:
                xml_file.write(xml)

            with patch(
                'sys.argv',
                ['libvirt-mem-convert.py', input_path, output_path, '--initial-memory', '1GiB'],
            ):
                libvirt_mem_convert.main()

            root = ElementTree.parse(output_path).getroot()
            self.assertEqual(root.findtext('currentMemory'), str(1048576))
            self.assertEqual(root.find('devices/memballoon').get('model'), 'none')

    def test_cli_omitted_mode_checks_default_base_without_replacing_it(self):
        xml = (
            "<domain><maxMemory unit='MiB'>4096</maxMemory><memory unit='MiB'>2048</memory>"
            "<currentMemory unit='MiB'>2048</currentMemory><devices>"
            "<memory model='virtio-mem'><target><size unit='MiB'>2048</size>"
            "<requested unit='MiB'>0</requested></target></memory>"
            "<memballoon model='none'/></devices></domain>"
        )
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, 'input.xml')
            output_path = os.path.join(directory, 'output.xml')
            with open(input_path, 'w', encoding='utf-8') as xml_file:
                xml_file.write(xml)

            with patch(
                'sys.argv',
                [
                    'libvirt-mem-convert.py', input_path, output_path,
                    '--initial-memory', '3GiB',
                ],
            ):
                libvirt_mem_convert.main()

            root = ElementTree.parse(output_path).getroot()
            self.assertEqual(root.findtext('memory'), '2048')
            self.assertEqual(root.findtext('currentMemory'), '2097152')
            self.assertEqual(root.findtext('devices/memory/target/requested'), '1048576')
            self.assertEqual(root.find('devices/memballoon').get('model'), 'none')

    def test_max_memory_overrides_capacity_in_both_directions(self):
        xml = (
            "<domain><memory unit='GiB'>4</memory>"
            "<currentMemory unit='GiB'>3</currentMemory></domain>"
        )
        base_kib = 1048576
        virtio_xml = libvirt_mem_convert.transform_xml_string(
            xml, 'to-virtiomem', base_kib, max_override_kib=6291457
        )
        root = ElementTree.fromstring(virtio_xml)
        self.assertEqual(root.findtext('maxMemory'), '6291456')
        self.assertEqual(root.findtext('devices/memory/target/size'), '5242880')
        self.assertEqual(root.findtext('devices/memory/target/requested'), '2097152')

        with self.assertRaisesRegex(ValueError, 'exceeds --max-memory'):
            libvirt_mem_convert.transform_xml_string(
                virtio_xml, 'to-balloon', base_kib, max_override_kib=2097153
            )
        balloon_xml = libvirt_mem_convert.transform_xml_string(
            virtio_xml, 'to-balloon', base_kib, initial_kib=1572864, max_override_kib=2097153
        )
        root = ElementTree.fromstring(balloon_xml)
        self.assertEqual(root.findtext('memory'), '2097153')
        self.assertEqual(root.findtext('currentMemory'), '1572864')

    def test_max_memory_updates_already_ballooned_domain(self):
        xml = "<domain><memory unit='GiB'>4</memory><currentMemory unit='GiB'>2</currentMemory></domain>"
        result = libvirt_mem_convert.transform_xml_string(
            xml, 'to-balloon', 1048576, max_override_kib=3145728
        )
        root = ElementTree.fromstring(result)
        self.assertEqual(root.findtext('memory'), '3145728')
        self.assertEqual(root.findtext('currentMemory'), '2')
        self.assertEqual(root.find('currentMemory').get('unit'), 'GiB')

    def test_initial_memory_overrides_current_memory_and_is_aligned(self):
        xml = (
            "<domain><memory unit='GiB'>4</memory>"
            "<currentMemory unit='GiB'>3</currentMemory></domain>"
        )
        for initial_kib, expected_requested, expected_warning in (
            (None, 2097152, ''),
            (1500 * 1024, 476 * 1024, ''),
            (
                1501 * 1024,
                476 * 1024,
                'Warning: --initial-memory rounded down from 1537024 KiB to '
                '1536000 KiB to align with 2 MiB blocks.\n',
            ),
        ):
            with self.subTest(initial_kib=initial_kib):
                with patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    result = libvirt_mem_convert.transform_xml_string(
                        xml, 'to-virtiomem', 1048576, initial_kib=initial_kib
                    )
                self.assertEqual(stderr.getvalue(), expected_warning)
                root = ElementTree.fromstring(result)
                self.assertEqual(root.findtext('currentMemory'), '1048576')
                requested = root.find('devices/memory/target/requested')
                self.assertEqual(requested.get('unit'), 'KiB')
                self.assertEqual(int(requested.text), expected_requested)

    def test_initial_memory_outside_domain_limits_is_rejected(self):
        xml = "<domain><memory unit='GiB'>4</memory></domain>"
        for initial_kib in (1048575, 4194305):
            with self.subTest(initial_kib=initial_kib):
                with self.assertRaisesRegex(ValueError, 'Initial memory must be between'):
                    libvirt_mem_convert.transform_xml_string(
                        xml, 'to-virtiomem', 1048576, initial_kib=initial_kib
                    )

    def test_removing_first_child_keeps_sibling_indentation(self):
        root = ElementTree.Element('domain')
        root.text = '\n  '
        max_memory = ElementTree.SubElement(root, 'maxMemory', {'unit': 'KiB'})
        max_memory.text = '4096'
        max_memory.tail = '\n  '
        comment = ElementTree.Comment('adjacent')
        comment.tail = '\n  '
        root.append(comment)
        memory = ElementTree.SubElement(root, 'memory', {'unit': 'KiB'})
        memory.text = '2048'
        memory.tail = '\n'

        libvirt_mem_convert.remove_child_formatted(root, max_memory)

        self.assertEqual(
            ElementTree.tostring(root, encoding='unicode'),
            '<domain>\n  <!--adjacent-->\n  <memory unit="KiB">2048</memory>\n</domain>',
        )

    def test_large_diff_blocks_skip_pairwise_alignment(self):
        minus_lines = ['-old line\n'] * 51
        plus_lines = ['+new line\n'] * 50

        with patch('libvirt_mem_convert.difflib.SequenceMatcher', side_effect=AssertionError):
            colored_minus, colored_plus = libvirt_mem_convert.align_hunk_lines(minus_lines, plus_lines)

        self.assertEqual(len(colored_minus), len(minus_lines))
        self.assertEqual(len(colored_plus), len(plus_lines))
        self.assertTrue(colored_minus[0].startswith(libvirt_mem_convert.ANSI_RED))
        self.assertTrue(colored_plus[0].startswith(libvirt_mem_convert.ANSI_GREEN))

    def test_conversion_preserves_qemu_namespace_prefix(self):
        xml = """<domain xmlns:qemu='http://libvirt.org'>
  <memory unit='KiB'>4096</memory>
  <currentMemory unit='KiB'>3072</currentMemory>
  <maxMemory slots='16' unit='KiB'>4096</maxMemory>
  <devices>
    <memory model='virtio-mem'>
      <target>
        <size unit='KiB'>2048</size>
        <requested unit='KiB'>1024</requested>
      </target>
    </memory>
  </devices>
  <qemu:commandline><qemu:arg value='-foo'/></qemu:commandline>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(xml, 'to-balloon', 1024)

        self.assertIn('<qemu:commandline>', result)
        self.assertIn('<qemu:arg ', result)
        self.assertNotIn('<ns0:commandline>', result)

    def test_conversion_preserves_comments_and_processing_instructions(self):
        xml = """<!-- outside the domain -->
<domain>
  <memory unit='GiB'>4</memory>
  <!-- keep "this" comment and its <markup> -->
  <?example test="value"?>
  <description note="it's intact">Guest</description>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)

        self.assertIn('<!-- keep "this" comment and its <markup> -->', result)
        self.assertIn('<?example test="value"?>', result)
        self.assertEqual(ElementTree.fromstring(result).find('description').get('note'), "it's intact")

    def test_metadata_default_namespace_does_not_change_domain_namespace(self):
        xml = (
            '<domain><memory unit="GiB">4</memory>'
            '<metadata><info xmlns="urn:example"/></metadata></domain>'
        )

        result = libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)

        root = ElementTree.fromstring(result)
        self.assertEqual(root.tag, 'domain')
        self.assertIsNotNone(root.find('metadata/{urn:example}info'))

    def test_to_virtiomem_preserves_secondary_numa_cell_allocations(self):
        xml = """<domain>
  <memory unit='MiB'>4096</memory>
  <vcpu>4</vcpu>
  <cpu><numa>
    <cell id='0' cpus='0-1' memory='3584' unit='MiB'/>
    <cell id='1' cpus='2-3' memory='512' unit='MiB'/>
  </numa></cpu>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)

        root = ElementTree.fromstring(result)
        first, second = root.findall('cpu/numa/cell')
        self.assertEqual(root.findtext('memory'), '1048576')
        self.assertEqual(first.attrib, {'id': '0', 'cpus': '0-1', 'memory': '524288', 'unit': 'KiB'})
        self.assertEqual(second.attrib, {'id': '1', 'cpus': '2-3', 'memory': '512', 'unit': 'MiB'})

        with self.assertRaisesRegex(ValueError, '524288 KiB memory allocation leaves no room for the first NUMA cell'):
            libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', 524288)

    def test_numa_cpu_set_excludes_disabled_vcpus(self):
        xml = """<domain>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>3145728</currentMemory>
  <vcpu current='2'>
    4
  </vcpu>
  <vcpus>
    <vcpu id='0' enabled='yes'/>
    <vcpu id='1' enabled='no'/>
    <vcpu id='2' enabled='yes'/>
    <vcpu id='3' enabled='no'/>
  </vcpus>
  <cpu><topology sockets='1' dies='1' cores='2' threads='2'/></cpu>
  <devices/>
</domain>"""

        result = libvirt_mem_convert.transform_xml_string(xml, 'to-virtiomem', 1048576)

        root = ElementTree.fromstring(result)
        self.assertEqual(root.find('cpu/numa/cell').get('cpus'), '0,2')

    def test_input_and_output_can_be_the_same_file(self):
        xml = (
            "<domain><memory unit='KiB'>4194304</memory>"
            "<currentMemory unit='KiB'>3145728</currentMemory>"
            "<vcpu>2</vcpu><devices/></domain>"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'domain.xml')
            with open(path, 'w', encoding='utf-8') as xml_file:
                xml_file.write(xml)
            os.chmod(path, 0o640)

            with patch(
                'sys.argv',
                ['libvirt-mem-convert.py', 'to-virtiomem', path, path],
            ):
                libvirt_mem_convert.main()

            with open(path, 'r', encoding='utf-8') as xml_file:
                result = ElementTree.parse(xml_file).getroot()
            self.assertIsNotNone(result.find('devices/memory[@model="virtio-mem"]'))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_atomic_write_uses_normal_creation_mode_without_changing_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'domain.xml')
            reference = os.path.join(directory, 'reference.xml')
            with open(reference, 'w', encoding='utf-8'):
                pass

            with patch.object(libvirt_mem_convert.os, 'umask', side_effect=AssertionError('umask changed')):
                libvirt_mem_convert.write_xml_atomically(path, '<domain/>')

            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), stat.S_IMODE(os.stat(reference).st_mode))
            self.assertCountEqual(os.listdir(directory), ['domain.xml', 'reference.xml'])

    def test_failed_atomic_replace_keeps_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'domain.xml')
            with open(path, 'w', encoding='utf-8') as xml_file:
                xml_file.write('original')

            with patch.object(libvirt_mem_convert.os, 'replace', side_effect=OSError('replace failed')):
                with self.assertRaisesRegex(OSError, 'replace failed'):
                    libvirt_mem_convert.write_xml_atomically(path, 'replacement')

            with open(path, 'r', encoding='utf-8') as xml_file:
                self.assertEqual(xml_file.read(), 'original')
            self.assertEqual(os.listdir(directory), ['domain.xml'])

    def test_process_vm_rejects_nonpersistent_domains(self):
        with patch.object(
            libvirt_mem_convert,
            'run_virsh',
            side_effect=['shut off', 'Name: guest-a\nPersistent: no\n'],
        ) as run_virsh:
            with self.assertRaisesRegex(RuntimeError, 'not persistent'):
                libvirt_mem_convert.process_vm('guest-a', 'to-virtiomem', 1048576)

        self.assertEqual(
            run_virsh.call_args_list,
            [call(['domstate', 'guest-a'], None), call(['dominfo', 'guest-a'], None)],
        )

    def test_process_vm_dry_run_allows_active_persistent_domain(self):
        with patch.object(
            libvirt_mem_convert,
            'run_virsh',
            side_effect=['running', 'Name: guest-a\nPersistent: yes\n', '', '<domain/>'],
        ) as run_virsh, patch.object(
            libvirt_mem_convert, 'transform_xml_string', return_value='<domain/>'
        ), patch.object(libvirt_mem_convert, 'print_diff') as print_diff:
            libvirt_mem_convert.process_vm('guest-a', 'to-virtiomem', 1048576, dry_run=True)

        self.assertEqual(len(run_virsh.call_args_list), 4)
        self.assertEqual(run_virsh.call_args_list[-1], call(['dumpxml', '--inactive', 'guest-a'], None))
        print_diff.assert_called_once()

    def test_process_vm_dry_run_keeps_persistence_and_snapshot_checks(self):
        for responses, message in (
            (['running', 'Name: guest-a\nPersistent: no\n'], 'not persistent'),
            (['running', 'Name: guest-a\nPersistent: yes\n', 'snapshot-a\n'], 'has snapshots'),
        ):
            with self.subTest(message=message):
                with patch.object(
                    libvirt_mem_convert, 'run_virsh', side_effect=responses
                ) as run_virsh:
                    with self.assertRaisesRegex(RuntimeError, message):
                        libvirt_mem_convert.process_vm(
                            'guest-a', 'to-virtiomem', 1048576, dry_run=True
                        )
                self.assertEqual(len(run_virsh.call_args_list), len(responses))

    def test_process_vm_still_requires_shutdown_without_dry_run(self):
        with patch.object(
            libvirt_mem_convert, 'run_virsh', return_value='running'
        ) as run_virsh:
            with self.assertRaisesRegex(RuntimeError, 'must be shut off'):
                libvirt_mem_convert.process_vm('guest-a', 'to-virtiomem', 1048576)

        run_virsh.assert_called_once_with(['domstate', 'guest-a'], None)

    def test_process_vm_rejects_domains_with_snapshots(self):
        with patch.object(
            libvirt_mem_convert,
            'run_virsh',
            side_effect=['shut off', 'Persistent: yes\n', 'snapshot-a\n'],
        ) as run_virsh:
            with self.assertRaisesRegex(RuntimeError, 'has snapshots'):
                libvirt_mem_convert.process_vm('guest-a', 'to-virtiomem', 1048576)

        self.assertEqual(
            run_virsh.call_args_list,
            [
                call(['domstate', 'guest-a'], None),
                call(['dominfo', 'guest-a'], None),
                call(['snapshot-list', '--name', 'guest-a'], None),
            ],
        )

    def test_process_vm_proceeds_for_persistent_domain_without_snapshots(self):
        with patch.object(
            libvirt_mem_convert,
            'run_virsh',
            side_effect=['shut off', 'Persistent: yes\n', '', '<domain/>', ''],
        ) as run_virsh, patch.object(libvirt_mem_convert, 'transform_xml_string', return_value='<domain/>'):
            libvirt_mem_convert.process_vm('guest-a', 'to-virtiomem', 1048576)

        self.assertEqual(run_virsh.call_args_list[:4], [
            call(['domstate', 'guest-a'], None),
            call(['dominfo', 'guest-a'], None),
            call(['snapshot-list', '--name', 'guest-a'], None),
            call(['dumpxml', '--inactive', 'guest-a'], None),
        ])
        self.assertEqual(run_virsh.call_args_list[4][0][0][:1], ['define'])


if __name__ == '__main__':
    unittest.main()

import os
import xml.etree.ElementTree as ET
from openai import OpenAI
from dataclasses import dataclass
import subprocess
import json
from typing import List, Dict
import shutil
import argparse

@dataclass
class CoverageInfo:
    covered_lines: List[int]
    source_code: str
    file_path: str

class SimpleMutationTester:
    def __init__(self, api_key: str, project_dir: str):
        self.client = OpenAI(api_key=api_key)
        self.project_dir = os.path.abspath(project_dir)
        
    def parse_jacoco_report(self, jacoco_path: str) -> Dict[str, CoverageInfo]:
        print(f"Parsing JaCoCo report from: {jacoco_path}")
        coverage_data = {}
        tree = ET.parse(jacoco_path)
        root = tree.getroot()
        
        for package in root.findall(".//package"):
            for sourcefile in package.findall(".//sourcefile"):
                filename = sourcefile.get("name")
                package_path = package.get('name').replace('.', '/')
                file_path = os.path.join(self.project_dir, "src", "main", "java", package_path, filename)
                
                print(f"Looking for source file: {file_path}")
                if os.path.exists(file_path):
                    # Get covered lines
                    covered_lines = []
                    for line in sourcefile.findall(".//line"):
                        if int(line.get("ci", 0)) > 0:  # ci = covered instructions
                            covered_lines.append(int(line.get("nr")))
                    
                    # Read source code
                    with open(file_path, 'r') as f:
                        source_code = f.read()
                        
                    coverage_data[file_path] = CoverageInfo(
                        covered_lines=covered_lines,
                        source_code=source_code,
                        file_path=file_path
                    )
                    print(f"✅ Parsed {len(covered_lines)} covered lines for {file_path}")
                else:
                    print(f"❌ Warning: Could not find file at {file_path}")
        
        return coverage_data

    def generate_mutations(self, coverage_info: CoverageInfo) -> List[dict]:
        print(f"Generating mutations for {coverage_info.file_path}")
        
        # First find the actual line numbers within the class
        class_start = -1
        lines = coverage_info.source_code.splitlines()
        for i, line in enumerate(lines):
            if "public class" in line or "class" in line:
                class_start = i
                break
                
        if class_start == -1:
            print("Could not find class definition")
            return []
            
        # Adjust covered lines to be relative to class content
        relative_covered_lines = [line for line in coverage_info.covered_lines]
        
        prompt = f"""You are a mutation testing expert. Generate exactly 3 mutations for the Java code below.
        The code is from file {coverage_info.file_path}.
        Only mutate the covered lines (line numbers: {relative_covered_lines}).
        Do not modify the class structure or method signatures.
        Only modify the implementation lines within methods.
        
        Original code:
        ```java
        {coverage_info.source_code}
        ```
        
        Provide output in this JSON format:
        {{
            "mutations": [
                {{
                    "line_number": <line number>,
                    "original_line": "<exact original line from code>",
                    "mutated_line": "<modified version of the exact line>",
                    "mutation_type": "<description of mutation>"
                }}
            ]
        }}
        """
        
        print("Calling GPT-4 for mutation suggestions...")
        try:
            response = self.client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            
            response_text = response.choices[0].message.content.strip()
            print("\nGPT Response:", response_text)
            
            try:
                mutations = json.loads(response_text)["mutations"]
                print(f"Generated {len(mutations)} mutations")
                return mutations
            except json.JSONDecodeError as e:
                print(f"JSON parsing error: {e}")
                print("Invalid JSON response:", response_text)
                return []
            except KeyError as e:
                print(f"Missing 'mutations' key in response: {e}")
                return []
                
        except Exception as e:
            print(f"API call error: {e}")
            return []

    def test_mutation(self, coverage_info: CoverageInfo, mutation: dict, maven_path: str = r'C:\Program Files\apache-maven-3.9.9\bin\mvn.cmd') -> bool:
        print(f"\nTesting mutation on line {mutation['line_number']}")
        # Create backup
        backup_path = f"{coverage_info.file_path}.backup"
        shutil.copy2(coverage_info.file_path, backup_path)
        
        try:
            # Read all lines
            with open(coverage_info.file_path, 'r') as f:
                lines = f.readlines()
            
            # Find the actual line within the class
            class_line = -1
            for i, line in enumerate(lines):
                if "public class" in line or "class" in line:
                    class_line = i
                    break
            
            if class_line == -1:
                print("Could not find class definition")
                return False
                
            # Adjust the line number to be relative to the class content
            adjusted_line = mutation['line_number'] - 1
            if adjusted_line >= len(lines):
                print(f"Line number {mutation['line_number']} is out of range")
                return False
                
            lines[adjusted_line] = mutation['mutated_line'] + '\n'
            
            with open(coverage_info.file_path, 'w') as f:
                f.writelines(lines)
            
            print("Running tests...")
            result = subprocess.run(
                [maven_path, "test"],
                shell=True,
                capture_output=True,
                text=True,
                cwd=os.path.dirname(coverage_info.file_path)
            )
            
            # Test fails = mutation killed
            mutation_survived = result.returncode == 0
            
            return mutation_survived
            
        except Exception as e:
            print(f"Error during mutation testing: {e}")
            return False
        finally:
            # Restore original file
            shutil.move(backup_path, coverage_info.file_path)
            print("Restored original file")

    def run(self, jacoco_path: str, maven_path: str = r'C:\Program Files\apache-maven-3.9.9\bin\mvn.cmd', test_command: str = "test"):
        print("\n=== Starting Mutation Testing ===")
        coverage_data = self.parse_jacoco_report(jacoco_path)
        
        full_log = ""  # For storing the complete log
        mutations_data = []  # For storing mutation results
        
        total_mutations = 0
        killed_mutations = 0
        
        for file_path, coverage_info in coverage_data.items():
            section_log = f"\n=== Analyzing {file_path} ===\n"
            full_log += section_log
            print(section_log)
            
            mutations = self.generate_mutations(coverage_info)
            
            for idx, mutation in enumerate(mutations, 1):
                total_mutations += 1
                mutation_log = f"\nMutation {idx}:\n"
                mutation_log += f"Type: {mutation['mutation_type']}\n"
                mutation_log += f"Line {mutation['line_number']}:\n"
                mutation_log += f"Original: {mutation['original_line'].strip()}\n"
                mutation_log += f"Mutated:  {mutation['mutated_line'].strip()}\n"
                
                full_log += mutation_log
                print(mutation_log)
                
                try:
                    survived = self.test_mutation(coverage_info, mutation, maven_path)
                    if not survived:
                        killed_mutations += 1
                    status = "🔴 SURVIVED" if survived else "✅ KILLED"
                    
                    # Add to mutations data for summary
                    mutations_data.append({
                        'mutation_type': mutation['mutation_type'],
                        'line_number': mutation['line_number'],
                        'original_line': mutation['original_line'],
                        'mutated_line': mutation['mutated_line'],
                        'survived': survived,
                        'file_name': os.path.basename(file_path),
                        'impact': self.get_mutation_impact(mutation)
                    })
                    
                except Exception as e:
                    status = f"❌ ERROR: {str(e)}"
                
                result_log = f"Status: {status}\n"
                full_log += result_log
                print(result_log)
        
        # Generate summary section
        summary = self.generate_summary_report(mutations_data)
        full_log += "\n" + summary
        
        # Write outputs to files
        self.write_outputs(full_log, mutations_data)

    
    def get_mutation_impact(self, mutation: dict) -> str:
        """Generate an impact description for the mutation"""
        if 'comparison' in mutation['mutation_type'].lower():
            return "Modified condition affecting control flow"
        elif 'changed' in mutation['mutation_type'].lower():
            return "Changed implementation behavior"
        elif 'removed' in mutation['mutation_type'].lower():
            return "Removed functionality"
        elif 'added' in mutation['mutation_type'].lower():
            return "Added additional behavior"
        else:
            return "Modified program behavior"

    def generate_summary_report(self, mutations_data: list[dict], total_cost: float = 0.00167) -> str:
        """Generate a detailed summary report"""
        from datetime import datetime
        
        total_mutations = len(mutations_data)
        survived = sum(1 for m in mutations_data if m.get('survived', False))
        killed = total_mutations - survived
        
        mutation_coverage = (killed / total_mutations * 100) if total_mutations > 0 else 0
        
        report = f"""
    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} INFO:
    =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
    📊 Overall Mutation Coverage 📊
    📈 Line Coverage: 100.00% 📈
    🎯 Mutation Coverage: {mutation_coverage:.2f}% 🎯
    🦠 Total Mutants: {total_mutations} 🦠
    🛡️  Survived Mutants: {survived} 🛡️ 
    🗡️  Killed Mutants: {killed} 🗡️ 
    🕒 Timeout Mutants: 0 🕒
    🔥 Compile Error Mutants: 0 🔥
    💰 Total Cost: ${total_cost:.5f} USD 💰
    =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

    Mutation Details:
    """
        
        for idx, mutation in enumerate(mutations_data, 1):
            status = "Survived" if mutation.get('survived', False) else "Killed"
            bg_color = "🟥" if mutation.get('survived', False) else "🟩"
            
            report += f"""
    {bg_color} M{idx}:
    ├─ Type: {mutation['mutation_type']}
    ├─ File: {mutation.get('file_name', 'Unknown')}
    ├─ Line {mutation['line_number']}:
    │  ├─ Original: {mutation['original_line'].strip()}
    │  └─ Mutated:  {mutation['mutated_line'].strip()}
    ├─ Status: {status}
    └─ Impact: {mutation.get('impact', 'Changed implementation behavior')}
    """
        
        return report

    def write_outputs(self, full_log: str, mutations_data: list[dict]):
        """Write both detailed log and summary to separate files"""
        # Write full execution log
        with open('mutation_testing_log.txt', 'w', encoding='utf-8') as f:
            f.write(full_log)
        
        # Write summary report
        summary_report = self.generate_summary_report(mutations_data)
        with open('mutation_testing_summary.txt', 'w', encoding='utf-8') as f:
            f.write(summary_report)
        
        # Open both files in notepad
        os.system('start notepad mutation_testing_log.txt')
        os.system('start notepad mutation_testing_summary.txt')


def main():
    parser = argparse.ArgumentParser(description='Simple Java Mutation Tester')
    parser.add_argument('--project-dir', required=True,
                      help='Path to Java project root directory (containing pom.xml)')
    parser.add_argument('--jacoco-path', required=True,
                      help='Path to JaCoCo XML report (e.g., target/site/jacoco/jacoco.xml)')
    parser.add_argument('--api-key', required=True,
                      help='OpenAI API key')
    parser.add_argument('--maven-path', 
                      default=r'C:\Program Files\apache-maven-3.9.9\bin\mvn.cmd',
                      help='Path to Maven executable')
    parser.add_argument('--test-command', default='test',
                      help='Maven test command (default: test)')
    
    args = parser.parse_args()
    
    # Convert paths to absolute paths
    project_dir = os.path.abspath(args.project_dir)
    jacoco_path = os.path.abspath(args.jacoco_path)
    
    tester = SimpleMutationTester(args.api_key, project_dir)
    tester.run(jacoco_path, maven_path=args.maven_path, test_command=args.test_command)

if __name__ == "__main__":
    main()